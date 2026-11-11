from flask import Flask, render_template, request, Response
import os
import requests
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

import hmac

# --- Hardcoded access credentials: no page is reachable without this login ---
_AUTH_USER = os.environ.get("APP_AUTH_USER", "apic")
_AUTH_PASS = os.environ.get("APP_AUTH_PASS", "")


def _check_auth(auth):
    if not _AUTH_PASS or not auth:
        return False
    user_ok = hmac.compare_digest(auth.username or "", _AUTH_USER)
    pass_ok = hmac.compare_digest(auth.password or "", _AUTH_PASS)
    return user_ok and pass_ok


@app.before_request
def _require_login():
    if not _check_auth(request.authorization):
        return Response(
            "Authentication required.\n",
            401,
            {"WWW-Authenticate": 'Basic realm="API Analytics"'},
        )



# IBM IAM Token URL
TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
API_KEY = os.environ.get("IBM_API_KEY", "")

# URLs
URL_STAGING = "https://api.8798-f464fa20.eu-de.ri.apiconnect.cloud.ibm.com/analytics/ibm-managed-analytics-service-v2/catalogs/tmfb/dev-catalog/events"
URL_PRODUCTION = "https://api.8798-f464fa20.eu-de.ri.apiconnect.cloud.ibm.com/analytics/ibm-managed-analytics-service-v2/catalogs/tmfb/gateway/events"

# Summary (dashboards/summary) endpoints
SUMMARY_STAGING = "https://api.8798-f464fa20.eu-de.ri.apiconnect.cloud.ibm.com/analytics/ibm-managed-analytics-service-v2/catalogs/tmfb/dev-catalog/dashboards/summary"
SUMMARY_PRODUCTION = "https://api.8798-f464fa20.eu-de.ri.apiconnect.cloud.ibm.com/analytics/ibm-managed-analytics-service-v2/catalogs/tmfb/gateway/dashboards/summary"

# Product call-count aggregation (server-side; all products, exact)
PRODUCT_DASH_STAGING = URL_STAGING.replace("/events", "/dashboards/product")
PRODUCT_DASH_PRODUCTION = URL_PRODUCTION.replace("/events", "/dashboards/product")

# Token cache
cached_token = None
token_expiry = 0

# Timeframe mapping
TIMEFRAME_MAP = {
    "1m": "last1minute",
    "5m": "last5minutes",
    "15m": "last15minutes",
    "1h": "last1hour",
    "4h": "last4hours",
    "12h": "last12hours",
    "24h": "last24hours",
    "7d": "last7days",
    "30d": "last30days",
    "all-event": "all"
}

def get_token():
    global cached_token, token_expiry

    if cached_token and time.time() < token_expiry:
        return cached_token

    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    data = {"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": API_KEY}

    res = requests.post(TOKEN_URL, headers=headers, data=data)
    res.raise_for_status()
    token_data = res.json()

    cached_token = token_data["access_token"]
    token_expiry = time.time() + token_data.get("expires_in", 3600) - 60

    return cached_token


@app.route("/analytics", methods=["GET", "POST"])
def analytics():
    # Determine environment
    env = request.args.get("env") or request.form.get("environment") or "production"
    ANALYTICS_URL = URL_STAGING if env == "staging" else URL_PRODUCTION

    response_data = None

    if request.method == "POST":
        params = {}

        # Timeframe
        timeframe = request.form.get("timeframe")
        params["timeframe"] = TIMEFRAME_MAP.get(timeframe, "last24hours")

        # App Name
        if request.form.get("app_name"):
            params["app_name"] = f"contains:{request.form.get('app_name')}"

        # Product (server-side filter: IBM accepts product_title)
        if request.form.get("product"):
            params["product_title"] = f"contains:{request.form.get('product')}"

        # Consumer Org / Client: IBM events API has no org query param, so pull
        # more rows and filter in-app by developer_org_title (done after fetch).
        client_org = (request.form.get("org_name") or "").strip()
        if client_org:
            params["limit"] = 1000

        # API Name
        if request.form.get("use_api_name"):
            api_name = request.form.get("api_name")
            if api_name:
                params["api_name"] = f"equals:{api_name}"

        # Global Txn ID
        if request.form.get("use_global_transaction_id"):
            gtid = request.form.get("global_transaction_id")
            if gtid:
                params["global_transaction_id"] = f"equals:{gtid}"

        # API Resource ID
        if request.form.get("use_api_resource_id"):
            api_resource = request.form.get("api_resource_id")
            if api_resource:
                params["api_resource_id"] = f"contains:{api_resource}"

        # Request Body
        if request.form.get("use_request_body"):
            req_body = request.form.get("request_body")
            if req_body:
                params["request_body"] = f"contains:{req_body}"

        # Response Body
        if request.form.get("use_response_body"):
            res_body = request.form.get("response_body")
            if res_body:
                params["response_body"] = f"contains:{res_body}"

        # Fields to fetch
        params["fields"] = (
            "developer_org_title,product_title,"
            "api_name,api_resource_id,app_name,request_http_headers,datetime,"
            "global_transaction_id,request_body,response_body,status_code,"
            "time_to_serve_request"
        )

        try:
            token = get_token()
        except Exception as e:
            return render_template("analytics.html", response={"error": str(e)}, environment=env, mode="logs")

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            res = requests.get(ANALYTICS_URL, headers=headers, params=params)
            res.raise_for_status()

            raw = res.json()
            events = raw.get("events", [])

            reordered_events = []

            for ev in events:
                # Extract X-Channel from headers array
                x_channel = None
                for header in ev.get("request_http_headers", []):
                    if "X-Channel" in header:
                        x_channel = header["X-Channel"]
                        break

                # Remove existing X-Channel in API response to avoid duplicates
                if "X-Channel" in ev:
                    del ev["X-Channel"]

                # Build ordered dict
                ordered = OrderedDict()
                ordered["developer_org_title"] = ev.get("developer_org_title")
                ordered["product_title"] = ev.get("product_title")
                ordered["api_name"] = ev.get("api_name")
                ordered["api_resource_id"] = ev.get("api_resource_id")
                ordered["app_name"] = ev.get("app_name")
                ordered["X-Channel"] = x_channel
                ordered["datetime"] = ev.get("datetime")
                ordered["global_transaction_id"] = ev.get("global_transaction_id")
                ordered["request_body"] = ev.get("request_body")
                ordered["response_body"] = ev.get("response_body")
                ordered["status_code"] = ev.get("status_code")
                ordered["time_to_serve_request"] = ev.get("time_to_serve_request")

                reordered_events.append(ordered)

            if client_org:
                _cl = client_org.lower()
                reordered_events = [
                    e for e in reordered_events
                    if _cl in (e.get("developer_org_title") or "").lower()
                ]

            response_data = {"events": reordered_events}

        except Exception as e:
            response_data = {"error": str(e)}

    return render_template("analytics.html", response=response_data, environment=env, mode="logs")


def _events_get(url, token, params):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _rank(count_map):
    """[{name, calls, pct}] sorted desc, dropping zeros."""
    items = sorted([(k, v) for k, v in count_map.items() if v], key=lambda x: -x[1])
    mx = items[0][1] if items else 1
    return [{"name": k, "calls": v, "pct": round(v * 100.0 / mx, 1)} for k, v in items]


def _product_calls_agg(url, token, tf, timeout=180):
    """Exact per-product call counts from IBM's server-side aggregation
    (every product, not a sample). Returns the same shape as _rank()."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = requests.get(url, headers=headers, params={"timeframe": tf}, timeout=timeout)
    r.raise_for_status()
    data = (r.json().get("top_products_by_calls") or {}).get("data") or []
    counts = {d.get("group"): d.get("value", 0) for d in data if d.get("group")}
    return _rank(counts)


def _calls_breakdown(events_url, token, tf, pages=4, page=1000, workers=28):
    """One sweep to discover app_name / product_title / api_name / api_resource_id,
    then exact counts for each app, each product, and each tree leaf. An API that has
    more than one resource (e.g. Inquiry vs Transfer) is split by api_resource_id.
    Returns (app_calls, product_calls, tree)."""
    def _page(i):
        return _events_get(events_url, token,
                           {"timeframe": tf, "limit": page, "offset": i * page,
                            "fields": "app_name,product_title,api_name,api_resource_id"})

    apps, products = set(), set()
    triple_res = {}  # (app, product, api) -> set(api_resource_id)
    with ThreadPoolExecutor(max_workers=pages) as ex:
        for d in ex.map(_page, range(pages)):
            for e in d.get("events", []):
                a = (e.get("app_name") or "").strip()
                p = (e.get("product_title") or "").strip()
                n = (e.get("api_name") or "").strip()
                r = (e.get("api_resource_id") or "").strip()
                if not a or a.lower() == "undefined":
                    continue
                apps.add(a)
                if p and p.lower() != "undefined" and n and n.lower() != "undefined":
                    products.add(p)
                    rs = triple_res.setdefault((a, p, n), set())
                    if r and r.lower() != "undefined":
                        rs.add(r)
                elif p and p.lower() != "undefined":
                    products.add(p)

    def _count(params):
        d = _events_get(events_url, token, dict(params, timeframe=tf, limit=1))
        return d.get("total", 0)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        app_f = {a: ex.submit(_count, {"app_name": f"equals:{a}"}) for a in apps}
        prod_f = {p: ex.submit(_count, {"product_title": f"equals:{p}"}) for p in products}
        tri_f, quad_f = {}, {}
        for (a, p, n), rs in triple_res.items():
            if len(rs) > 1:
                for r in rs:
                    quad_f[(a, p, n, r)] = ex.submit(_count, {
                        "app_name": f"equals:{a}", "product_title": f"equals:{p}",
                        "api_name": f"equals:{n}", "api_resource_id": f"equals:{r}"})
            else:
                tri_f[(a, p, n)] = ex.submit(_count, {
                    "app_name": f"equals:{a}", "product_title": f"equals:{p}",
                    "api_name": f"equals:{n}"})
        app_counts = {a: f.result() for a, f in app_f.items()}
        prod_counts = {p: f.result() for p, f in prod_f.items()}
        tri_counts = {k: f.result() for k, f in tri_f.items()}
        quad_counts = {k: f.result() for k, f in quad_f.items()}

    def _op(r):
        parts = r.split(":")
        return f"{parts[2]} {parts[3]}" if len(parts) >= 4 else r

    nested = {}  # app -> product -> api -> {calls, resources}
    for (a, p, n), c in tri_counts.items():
        if c:
            nested.setdefault(a, {}).setdefault(p, {})[n] = {"calls": c, "resources": None}
    for (a, p, n, r), c in quad_counts.items():
        if not c:
            continue
        api = nested.setdefault(a, {}).setdefault(p, {}).setdefault(n, {"calls": 0, "resources": []})
        if api["resources"] is None:
            api["resources"] = []
        api["resources"].append({"name": _op(r), "calls": c})
        api["calls"] += c

    tree = []
    for a, prods in nested.items():
        plist = []
        for p, apis in prods.items():
            apilist = []
            for n, info in apis.items():
                res = info["resources"]
                if res:
                    res.sort(key=lambda x: -x["calls"])
                apilist.append({"name": n, "calls": info["calls"], "resources": res})
            apilist.sort(key=lambda x: -x["calls"])
            plist.append({"product": p, "calls": sum(x["calls"] for x in apilist), "apis": apilist})
        plist.sort(key=lambda x: -x["calls"])
        tree.append({"app": a, "calls": sum(x["calls"] for x in plist), "products": plist})
    tree.sort(key=lambda x: -x["calls"])

    return _rank(app_counts), _rank(prod_counts), tree


@app.route("/summary", methods=["GET", "POST"])
def summary():
    env = request.args.get("env") or request.form.get("environment") or "production"
    SUMMARY_URL = SUMMARY_STAGING if env == "staging" else SUMMARY_PRODUCTION

    summary_data = None
    timeframe = "24h"

    if request.method == "POST":
        timeframe = request.form.get("timeframe") or "24h"
        tf = TIMEFRAME_MAP.get(timeframe, "last24hours")

        def _num(v):
            return v.get("data") if isinstance(v, dict) else None

        try:
            token = get_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            res = requests.get(SUMMARY_URL, headers=headers, params={"timeframe": tf})
            res.raise_for_status()
            raw = res.json()

            art = _num(raw.get("avg_response_time"))
            summary_data = {
                "total_apis": _num(raw.get("total_apis")),
                "total_products": _num(raw.get("total_products")),
                "total_applications": _num(raw.get("total_applications")),
                "total_consumer_orgs": _num(raw.get("total_consumer_orgs")),
                "total_api_calls": _num(raw.get("total_api_calls")),
                "transactions_per_second": _num(raw.get("transactions_per_second")),
                "avg_response_time": round(art, 2) if isinstance(art, (int, float)) else art,
                "status_codes": (raw.get("status_codes") or {}).get("data") or [],
            }

            try:
                events_url = URL_STAGING if env == "staging" else URL_PRODUCTION
                product_dash = PRODUCT_DASH_STAGING if env == "staging" else PRODUCT_DASH_PRODUCTION
                # product list from server-side aggregation (all products, exact),
                # run in parallel with the per-app tree so it adds no extra wait
                with ThreadPoolExecutor(max_workers=2) as _ex:
                    _f_prod = _ex.submit(_product_calls_agg, product_dash, token, tf)
                    app_calls, sampled_prod, tree = _calls_breakdown(events_url, token, tf)
                    try:
                        product_calls = _f_prod.result()
                    except Exception:
                        product_calls = sampled_prod  # fallback: sampled products
                summary_data["app_calls"] = app_calls
                summary_data["product_calls"] = product_calls
                summary_data["tree"] = tree
            except Exception:
                summary_data["app_calls"] = []
                summary_data["product_calls"] = []
                summary_data["tree"] = []
        except Exception as e:
            summary_data = {"error": str(e)}

    return render_template(
        "analytics.html",
        mode="analytics",
        summary=summary_data,
        summary_timeframe=timeframe,
        environment=env,
        response=None,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
