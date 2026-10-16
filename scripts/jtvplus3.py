import os
import re
import json
import base64
import time
import sys
import requests
from typing import Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, unquote

CHANNELS_URL = "https://sportlink-jtv.pages.dev/ztv.json"
COOKIE_URL = "https://allinonereborn2.online/jstrweb2/cookies.json"
SPORTS_COOKIE_URL = "https://allinonereborn2.online/jtv-fetch/jstarcookie/cookie.json"

M3U_FILE = "jtvplus3.m3u"
JSON_FILE = "jtv2.json"

USER_AGENT = "Sayan10"
MAX_RETRIES = 4
RETRY_DELAY = 5


# ---------------- RETRY FETCHER ----------------
def get_json(url: str) -> Any:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            fresh_url = f"{url}{'&' if '?' in url else '?'}t={int(time.time() * 1000)}"
            resp = requests.get(
                fresh_url,
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if data is None or data == [] or data == {}:
                raise Exception("Empty response")
            print(f"[OK] Fetched {url} (attempt {attempt})")
            return data
        except Exception as e:
            last_error = e
            print(f"[WARN] Attempt {attempt}/{MAX_RETRIES} failed for {url}: {e}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

    raise Exception(f"Failed to fetch {url} after {MAX_RETRIES} attempts: {last_error}")


# ---------------- NORMAL COOKIE ----------------
def get_normal_cookie() -> str:
    try:
        data = get_json(COOKIE_URL)
    except Exception as e:
        print(f"[WARN] Cookie fetch failed: {e}")
        return ""

    if isinstance(data, str):
        return data
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("cookie"):
                return item["cookie"]
        return ""
    if isinstance(data, dict):
        return data.get("cookie") or ""
    return ""


# ---------------- COOKIE EXTRACTION ----------------
def _extract_cookie_from_url(url: str) -> Tuple[str, str]:
    """
    Extract embedded token/cookie params from a JioTV URL.
    """
    if not url:
        return url, ""

    parsed = urlparse(url)
    if not parsed.query:
        return url, ""

    params = parse_qs(parsed.query, keep_blank_values=True)

    cookie_name = ""
    cookie_raw = ""

    for key in ("__hdnea__", "__cookie__", "cookie", "cookies"):
        if key in params:
            cookie_raw = params.pop(key)[0]
            cookie_name = key
            break

    if not cookie_raw:
        m = re.search(r"(?:^|&)(__hdnea__|__cookie__|cookie|cookies)=([^&]+)", parsed.query)
        if m:
            cookie_name = m.group(1)
            cookie_raw = m.group(2)
            params = parse_qs(
                re.sub(r"(?:^|&)" + re.escape(cookie_name) + r"=[^&]+", "", parsed.query).lstrip("&"),
                keep_blank_values=True,
            )

    if not cookie_raw:
        return url, ""

    cookie_val = unquote(cookie_raw)
    cookie_string = f"{cookie_name}={cookie_val}"

    new_query = urlencode(params, doseq=True)
    clean_url = urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, new_query, parsed.fragment,
    ))
    return clean_url, cookie_string


# ---------------- COOKIE EXPIRY ----------------
def get_cookie_expiry_details(cookie: str) -> Tuple[int, str]:
    """
    Parse `exp=<unix_ts>` from an __hdnea__ cookie.
    Returns (timestamp, human_readable_ist_string).
    """
    if not cookie:
        return 0, ""
    m = re.search(r"exp=(\d+)", cookie)
    if not m:
        return 0, ""
    try:
        exp = int(m.group(1))
        dt_utc = datetime.fromtimestamp(exp, tz=timezone.utc)
        dt_ist = dt_utc + timedelta(hours=5, minutes=30)
        hour12 = dt_ist.hour % 12 or 12
        ampm = "AM" if dt_ist.hour < 12 else "PM"
        human = (
            f"{dt_ist.day} {dt_ist.strftime('%b %Y')}, "
            f"{hour12:02d}:{dt_ist.minute:02d}:{dt_ist.second:02d} {ampm} IST"
        )
        return exp, human
    except Exception:
        return 0, ""


# ---------------- SPORTS DATA ----------------
def get_sports_data() -> Dict[str, Any]:
    empty = {"sportsIds": set(), "sportsCookies": {}}

    try:
        data = get_json(SPORTS_COOKIE_URL)
    except Exception as e:
        print(f"[WARN] Sports fetch failed: {e}")
        return empty

    sports_cookies: Dict[str, Dict[str, str]] = {}
    results = (data.get("successful_results") or []) + (data.get("failed_results") or [])

    for item in results:
        if not isinstance(item, dict):
            continue
        channel_id = item.get("channel_id")
        if not channel_id:
            continue
        final_url = (
            item.get("final_url")
            or (item.get("error_details") or {}).get("final_url")
            or ""
        )
        if not final_url:
            continue

        final_url = re.sub(r"/output/", "/WDVLive/", final_url, count=1, flags=re.I)
        clean_url, embedded_cookie = _extract_cookie_from_url(final_url)

        sports_cookies[str(channel_id)] = {
            "url": clean_url,
            "cookie": embedded_cookie,
        }

    print(f"[INFO] Sports URLs loaded: {len(sports_cookies)}")
    return {"sportsIds": set(sports_cookies.keys()), "sportsCookies": sports_cookies}


# ---------------- HELPERS ----------------
def extract_all_keys(channel) -> List[Dict[str, str]]:
    """
    Extract ALL ClearKey pairs from a channel.
    Returns list of dicts: [{"key_id": "...", "key": "..."}, ...]

    Supported formats:
      1) channel["keys"] = [{"keyId": "...", "key": "..."}, ...]   <-- primary format
      2) channel["keys"] = [{"key_id": "...", "key": "..."}, ...]
      3) channel["keys"] = ["kid:key", "kid2:key2", ...]
      4) channel["clearkey"] = {"kid": "key", ...}
      5) channel["keyId"] + channel["key"]
      6) channel["key_ids"] + channel["key_values"]
    """
    keys: List[Dict[str, str]] = []

    # 1, 2, 3) keys list (list of dicts OR list of "kid:key" strings)
    keys_list = channel.get("keys")
    if isinstance(keys_list, list):
        for item in keys_list:
            if isinstance(item, dict):
                kid = (
                    item.get("keyId")
                    or item.get("key_id")
                    or item.get("kid")
                    or item.get("keyID")
                )
                kval = item.get("key") or item.get("k") or item.get("value")
                if kid and kval:
                    keys.append({"key_id": str(kid), "key": str(kval)})
            elif isinstance(item, str) and ":" in item:
                kid, kval = item.split(":", 1)
                kid, kval = kid.strip(), kval.strip()
                if kid and kval:
                    keys.append({"key_id": kid, "key": kval})

    # 4) clearkey dict
    if not keys:
        clearkey = channel.get("clearkey")
        if isinstance(clearkey, dict) and clearkey:
            for kid, kval in clearkey.items():
                if kid and kval:
                    keys.append({"key_id": str(kid), "key": str(kval)})

    # 5) keyId + key singletons
    if not keys:
        kid = channel.get("keyId") or channel.get("key_id")
        kval = channel.get("key")
        if kid and kval:
            keys.append({"key_id": str(kid), "key": str(kval)})

    # 6) key_ids list + key_values list
    if not keys:
        key_ids = channel.get("key_ids")
        key_vals = channel.get("key_values")
        if (
            isinstance(key_ids, list)
            and isinstance(key_vals, list)
            and len(key_ids) == len(key_vals)
        ):
            for kid, kval in zip(key_ids, key_vals):
                if kid and kval:
                    keys.append({"key_id": str(kid), "key": str(kval)})

    # Deduplicate preserving order
    seen = set()
    unique = []
    for k in keys:
        sig = (k["key_id"], k["key"])
        if sig not in seen:
            seen.add(sig)
            unique.append(k)

    return unique


def _clean_base_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def resolve_channel(channel, normal_cookie, sports_cookies):
    """
    Returns (final_url, cookie_value, is_sports).
    """
    channel_id = str(channel.get("id") or "")

    if channel_id in sports_cookies:
        entry = sports_cookies[channel_id]
        if isinstance(entry, str):
            url, cookie = _extract_cookie_from_url(entry)
            return url, cookie or normal_cookie, True
        url = entry.get("url", "")
        cookie = entry.get("cookie", "")
        if not cookie:
            cookie = normal_cookie
        return url, cookie, True

    raw_url = channel.get("url") or ""
    clean_url, embedded_cookie = _extract_cookie_from_url(raw_url)
    if embedded_cookie:
        return clean_url, embedded_cookie, False
    return _clean_base_url(raw_url), normal_cookie, False


# ---------------- M3U ENTRY ----------------
def create_channel_entry(channel, normal_cookie="", sports_cookies=None):
    if sports_cookies is None:
        sports_cookies = {}

    channel_id = str(channel.get("id") or "")
    name       = channel.get("name") or ""
    logo       = channel.get("logo") or ""
    group      = channel.get("group") or channel.get("category") or "Other"
    raw_url    = channel.get("url") or ""

    all_keys = extract_all_keys(channel)

    final_url, cookie_to_use, _is_sports = resolve_channel(
        channel, normal_cookie, sports_cookies
    )

    lines = []

    lines.append(
        f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{name}" '
        f'tvg-logo="{logo}" group-title="{group}",{name}'
    )

    is_mpd = (
        channel.get("type") == "dash"
        or bool(re.search(r"\.mpd(?:\?|$)", final_url, re.I))
        or bool(re.search(r"\.mpd(?:\?|$)", raw_url, re.I))
    )

    if is_mpd:
        lines.append("#KODIPROP:inputstream=inputstream.adaptive")
        lines.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
        if all_keys:
            lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            # Multiple keys joined with comma: kid1:key1,kid2:key2
            key_pairs = ",".join(f"{k['key_id']}:{k['key']}" for k in all_keys)
            lines.append(f"#KODIPROP:inputstream.adaptive.license_key={key_pairs}")
        elif channel.get("license_url"):
            lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            lines.append(f'#KODIPROP:inputstream.adaptive.license_key={channel["license_url"]}')

    if cookie_to_use:
        stream_headers = (
            f"User-Agent={USER_AGENT}"
            f"&Referer=https://www.jiotv.com/"
            f"&Origin=https://www.jiotv.com/"
            f"&Cookie={cookie_to_use}"
        )
        lines.append("#KODIPROP:inputstream.adaptive.stream_headers=" + stream_headers)

        lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        lines.append("#EXTVLCOPT:http-referrer=https://www.jiotv.com/")
        lines.append(f"#EXTVLCOPT:http-cookie={cookie_to_use}")

        exthttp = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.jiotv.com/",
            "Origin": "https://www.jiotv.com/",
            "Cookie": cookie_to_use,
        }
        lines.append(f'#EXTHTTP:{json.dumps(exthttp, ensure_ascii=False)}')
    else:
        lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
        lines.append("#EXTVLCOPT:http-referrer=https://www.jiotv.com/")

    lines.append(final_url)

    return "\n".join(lines)


# ---------------- JSON ENTRY ----------------
def build_channel_object(channel, normal_cookie="", sports_cookies=None):
    if sports_cookies is None:
        sports_cookies = {}

    all_keys = extract_all_keys(channel)

    final_url, cookie_to_use, _is_sports = resolve_channel(
        channel, normal_cookie, sports_cookies
    )

    exp_ts, exp_ist = get_cookie_expiry_details(cookie_to_use)

    # Primary key (first) for legacy key_id/key fields
    primary = all_keys[0] if all_keys else {"key_id": None, "key": None}

    # sportlink: all key pairs as "kid:key" strings (when >1 key present)
    sportlink = None
    if len(all_keys) > 1:
        sportlink = [f"{k['key_id']}:{k['key']}" for k in all_keys]

    return {
        "id":                str(channel.get("id") or ""),
        "name":              channel.get("name") or "",
        "stream_url":        final_url,
        "cookie":            cookie_to_use,
        "cookie_expire":     exp_ts,
        "cookie_expire_ist": exp_ist,
        "key_id":            primary["key_id"],
        "key":               primary["key"],
        "sportlink":         sportlink,
    }


# ---------------- VALIDATE ----------------
def validate(channels, m3u_entries, json_entries):
    errors = []
    if not channels:
        errors.append("Channels list is empty")
    if not m3u_entries:
        errors.append("M3U output is empty")
    if not json_entries:
        errors.append("JSON output is empty")
    if len(m3u_entries) != len(json_entries):
        errors.append(f"Count mismatch: {len(m3u_entries)} M3U vs {len(json_entries)} JSON")
    if len(m3u_entries) < len(channels) * 0.8:
        errors.append(f"Too many channels dropped: expected ~{len(channels)}, got {len(m3u_entries)}")

    if errors:
        for e in errors:
            print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Validation passed: {len(m3u_entries)} channels")


# ---------------- GITHUB UPLOAD ----------------
def to_base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def upload_to_github(filename: str, content: str):
    repo_owner = os.environ.get("GITHUB_OWNER") or os.environ.get("GITHUB_REPOSITORY", "").split("/")[0]
    repo_name  = os.environ.get("GITHUB_REPO")  or os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
    token      = os.environ.get("GITHUB_TOKEN")

    if not all([repo_owner, repo_name, token]):
        print(f"[WARN] GitHub credentials missing — skipping upload for {filename}")
        return

    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Python-Script",
        "Accept": "application/vnd.github.v3+json",
    }

    existing = requests.get(api_url, headers=headers)
    sha = None
    if existing.status_code == 200:
        existing_json = existing.json()
        sha = existing_json.get("sha")
        existing_content = ""
        if existing_json.get("content"):
            existing_content = base64.b64decode(existing_json["content"]).decode("utf-8")
        if existing_content and existing_content.strip().replace("\r", "") == content.strip().replace("\r", ""):
            print(f"[INFO] No changes in {filename} — skipping commit")
            return

    payload = {
        "message": f"Auto-update {filename}: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "content": to_base64(content),
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(api_url, headers=headers, json=payload)
    if resp.ok:
        print(f"[OK] Uploaded {filename} to GitHub ({resp.status_code})")
    else:
        print(f"[ERROR] GitHub upload failed for {filename}: {resp.status_code} — {resp.text}", file=sys.stderr)


# ---------------- MAIN ----------------
def main():
    print(f"[START] {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")

    channels = get_json(CHANNELS_URL)
    if isinstance(channels, dict):
        channels = channels.get("channels") or channels.get("data") or []
    print(f"[INFO] Channels loaded: {len(channels)}")

    normal_cookie = get_normal_cookie()
    print(f"[INFO] Cookie: {'found' if normal_cookie else 'not found'}")

    sports_data = get_sports_data()

    m3u_entries  = []
    json_entries = []

    for ch in channels:
        try:
            m3u_entries.append(create_channel_entry(ch, normal_cookie, sports_data["sportsCookies"]))
            json_entries.append(build_channel_object(ch, normal_cookie, sports_data["sportsCookies"]))
        except Exception as e:
            print(f"[WARN] Skipped channel '{ch.get('name', '?')}': {e}", file=sys.stderr)

    validate(channels, m3u_entries, json_entries)

    timestamp   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m3u_content = f'#EXTM3U x-tvg-url="" updated="{timestamp}"\n\n' + "\n\n".join(m3u_entries)
    json_content = json.dumps(json_entries, indent=2, ensure_ascii=False)

    with open(M3U_FILE, "w", encoding="utf-8") as f:
        f.write(m3u_content)
    print(f"[INFO] M3U saved → {M3U_FILE}")

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        f.write(json_content)
    print(f"[INFO] JSON saved → {JSON_FILE}")

    upload_to_github(M3U_FILE, m3u_content)
    upload_to_github(JSON_FILE, json_content)

    print("[DONE] All outputs written successfully.")


if __name__ == "__main__":
    main()
