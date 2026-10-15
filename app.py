from flask import Flask, render_template, request, Response
import os
import requests
import time
from collections import OrderedDict

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
            return render_template("analytics.html", response={"error": str(e)}, environment=env)

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

    return render_template("analytics.html", response=response_data, environment=env)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
