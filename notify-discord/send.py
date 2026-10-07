"""Best-effort deployment announcements; never print webhook credentials."""

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def payload(env):
    target = env.get("NOTIFY_ENVIRONMENT", "")
    status = env.get("NOTIFY_STATUS", "")
    if target not in {"production", "preview"} or status not in {"success", "failure"}:
        raise ValueError("invalid deployment result")
    if target == "preview" and status == "failure":
        return None
    app = env.get("NOTIFY_APP", "").strip()
    revision = env.get("NOTIFY_REVISION", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if not app or not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise ValueError("application and full commit SHA required")
    fields = [
        {"name": "Repository", "value": repo[:1024] or "unknown"},
        {"name": "Commit", "value": f"[{revision[:7]}]({server}/{repo}/commit/{revision})"},
        {"name": "Workflow", "value": f"[View run]({server}/{repo}/actions/runs/{env.get('GITHUB_RUN_ID', '')})"},
    ]
    if target == "preview":
        pr = env.get("NOTIFY_PR", "")
        url = env.get("NOTIFY_PREVIEW_URL", "")
        if not re.fullmatch(r"[1-9][0-9]*", pr) or not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?/?", url):
            raise ValueError("preview PR and HTTPS URL required")
        fields.extend([
            {"name": "Pull request", "value": f"[#{pr}]({server}/{repo}/pull/{pr})"},
            {"name": "Preview", "value": url[:1024]},
        ])
    details = env.get("NOTIFY_DETAILS", "").strip()
    if details:
        fields.append({"name": "Details", "value": details[:1024]})
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": f"{app} · {target} · {status}"[:256],
            "color": 3066993 if status == "success" else 15158332,
            "fields": fields,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }],
    }


def send(env, opener=None, sleep=time.sleep):
    body = payload(env)
    if body is None:
        return
    url = env.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("::warning::Discord notification skipped: DISCORD_WEBHOOK_URL is not configured.")
        return
    if not re.fullmatch(r"https://(?:discord\.com|discordapp\.com)/api/webhooks/[0-9]+/[A-Za-z0-9_-]+", url):
        raise ValueError("invalid webhook URL")
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "komizo-actions-deploy-notifications",
    }, method="POST")
    for attempt in range(3):
        try:
            with opener.open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise ValueError("webhook rejected")
            print("Discord deployment notification delivered.")
            return
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise ValueError("webhook rejected") from None
            # Bound even an excessive Retry-After; never log response bodies.
            try:
                delay = min(5, max(1, float(error.headers.get("Retry-After", "2"))))
            except ValueError:
                delay = 2
            sleep(delay)
    raise ValueError("webhook delivery failed")


def main():
    try:
        send(os.environ)
    except Exception:
        # Exceptions can contain a webhook URL/token or untrusted HTTP body.
        print("::warning::Discord notification could not be delivered; deployment result is unchanged.")


if __name__ == "__main__":
    main()
