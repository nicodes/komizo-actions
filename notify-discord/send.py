"""Best-effort deployment announcements; never print webhook credentials."""

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
    revision = env.get("NOTIFY_REVISION", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise ValueError("repository and full commit SHA required")
    links = []
    url = env.get("NOTIFY_DEPLOYMENT_URL", "")
    if target == "preview":
        pr = env.get("NOTIFY_PR", "")
        url = url or env.get("NOTIFY_PREVIEW_URL", "")
        if not re.fullmatch(r"[1-9][0-9]*", pr) or not url:
            raise ValueError("preview PR and HTTPS URL required")
        links.append(f"[PR]({server}/{repo}/pull/{pr})")
    if url:
        if not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?/?", url):
            raise ValueError("HTTPS deployment URL required")
        links.insert(0, f"[View]({url})")
    links.append(f"[Run]({server}/{repo}/actions/runs/{env.get('GITHUB_RUN_ID', '')})")
    emoji = "❌" if status == "failure" else ("🚀" if target == "production" else "🧪")
    actor = env.get("GITHUB_ACTOR", "")
    if actor:
        if not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", actor):
            raise ValueError("invalid GitHub actor")
        # Escape bot account brackets so Discord renders the full handle.
        handle = actor.replace("[", "\\[").replace("]", "\\]")
        links.insert(0, f"[@{handle}]({server}/{actor})")
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "color": 3066993 if status == "success" else 15158332,
            "description": f"{emoji} **{repo.split('/')[-1]}** · " + " · ".join(links),
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
