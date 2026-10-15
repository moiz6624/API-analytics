# API Analytics

A small Flask web app that queries **IBM API Connect** analytics events for the
`tmfb` catalog and renders them in the browser with rich filtering.

## Features

- **Password-protected** â€” HTTP Basic Auth gates every route; no page is reachable without logging in.
- Filter by **Timeframe**, **Consumer Org (Client)**, **Product**, and **App Name**.
- **Advanced filters**: API Name, API Resource ID, Global Transaction ID, request-body and response-body text match.
- Each result row shows its **client** and **product**.
- Staging / Production environment toggle.

## Configuration

All secrets are supplied **at runtime via environment variables** â€” nothing
sensitive is stored in this repository.

| Variable | Required | Description |
|----------|----------|-------------|
| `IBM_API_KEY` | yes | IBM Cloud API key used to obtain an IAM access token. |
| `APP_AUTH_USER` | no (default `apic`) | Username required to access the app. |
| `APP_AUTH_PASS` | yes | Password required to access the app. If unset, the app denies all requests (fail-closed). |

See [`.env.example`](.env.example) for the expected format. Create your own
private env file from it and **never commit real values**.

## Run with Docker

```bash
docker build -t api-analytics:latest .

docker run -d --name api-analytics --restart unless-stopped \
  -p 5000:5000 \
  --env-file /path/to/your-private.env \
  api-analytics:latest
```

Then open: `http://<host>:5000/analytics`

## Run locally (without Docker)

```bash
pip install -r requirements.txt

export IBM_API_KEY=your-ibm-cloud-api-key
export APP_AUTH_USER=apic
export APP_AUTH_PASS=choose-a-password

python app.py
```

## Security notes

- The app runs on Flask's built-in development server. For production, put it
  behind a proper WSGI server (gunicorn / waitress).
- HTTP Basic Auth transmits credentials base64-encoded (not encrypted). Serve
  over **HTTPS** if the app is reachable beyond a trusted network.
- Keep your IBM API key and app password only in your private env file.

