# ydl — pull your folders out of Yandex.Disk onto your own server

[Русский](README.md) · **English** · [MIT License](LICENSE) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

> ⚠️ **Status: alpha (early version).** This project was written quickly and is released **as is**. Expect bugs; features may break whenever Yandex changes
> something. Use it **at your own risk**: do not keep the only copy of your data on it, verify the results, keep backups. No support or fix timelines are promised.

> **Unofficial project, not affiliated with Yandex.** Meant for downloading **your own** data (or data you are entitled to).
> Respect Yandex's terms of service and copyright law. See the Disclaimer below.

`ydl` is a small self-hosted web app (one Docker container, a few commands) that downloads whole **public-link folders** from
Yandex.Disk to your server. Handy when the desktop client stalls or went paid-only (free desktop sync ended on 3 June 2026),
when WebDAV / `rclone` stop working, or when the web UI gives you no "download folder" button.

*The web interface is currently in Russian only.*

## Features
- Add many links at once (one row per link, optional password per link); pick which files to download; start when ready.
- Parallel downloads with resume, size + MD5 verification, and a scheduler that deals with Yandex storage nodes that hang
  (it prefers files on nodes that recently worked and retries the rest later).
- Never fights Yandex's limits: pauses on 429 / 503 / captcha and resumes by itself.
- Works with **password-protected links** (headless Chromium logs in; see the Disclaimer).
- Built-in file manager: browse, search, download, make `.zip` archives next to the folder (re-downloadable), delete downloaded data
  to free space (deleted files are excluded from the queue so they are not fetched again).
- Password login page, HTTPS in one command: Let's Encrypt certificate for a bare **IP address** (VPS) or for your **domain** (Caddy).
- Mobile-friendly UI, download history charts, installer that can even install Docker for you.

## Install
Requirements: a Linux server with `sudo`/root and `git` (`sudo apt-get install -y git`). Docker is installed by the installer if missing.
Log in to the server over ssh and run two commands:
```bash
git clone https://github.com/ferrykit/ydl && cd ydl
YDL_IMAGE=ghcr.io/ferrykit/ydl:latest ./install.sh
```
The installer asks a few questions (every one has a sensible default, press Enter), pulls the ready-made image and starts the service.
At the end the installer prints the URL to open (and the password it generated). Day-to-day: `./ydl help`
(`status`, `logs`, `update`, `password`, `https ip|domain NAME|off`, `slots N`, `info`).

## How to use
1. In Yandex.Disk create a **public link for each folder** you want (there is no link for the whole disk; for loose files in the root,
   share each file or move them into a new folder and share that). If your plan allows it, add a link password and an expiry.
2. In ydl click **Add links**, paste the links (one per row; password in the second field if the link has one).
3. Wait for the folder structure to load, review the file tree, press **Start**.
4. When a link is done, **close the public link** on Yandex.Disk.
5. Take the files: the **Files** page (download, zip archives) or `rsync` from the server. Delete what you copied to free space.

## Security in short
Login with lockout, CSRF protection (custom header + `Origin`/`Sec-Fetch-Site`), DNS-rebinding protection (`Host` allow-list,
see `YDL_ALLOWED_HOSTS`), downloaded HTML/SVG is never rendered by the browser (download-only, CSP sandbox), only HTTPS downloads from
Yandex domains, read-only unprivileged containers, service runs as non-root. Details and the threat model: [SECURITY.md](SECURITY.md).
Please report vulnerabilities privately, not in public issues.

## Settings
Everything lives in `.env` (see `.env.example`): download folder, slots (parallel downloads, default 8), password, HTTPS mode,
minimum free disk space (`YDL_MIN_FREE_GB`, downloads pause below it), allowed host names. The full documentation is in
[README.md](README.md) (Russian).

## Disclaimer
- **Alpha, at your own risk.** The code was written quickly and is provided as is, with no guarantee of correctness, security or data safety. You run it on your own server under your own responsibility;
  the author is not liable for lost or corrupted data, account restrictions or any other consequences. Do not rely on it as the only storage and verify what you download.
- **Unofficial.** Not affiliated with or endorsed by Yandex. Product names belong to their owners and are mentioned only to describe compatibility.
- **Your data only.** Use it for data you own or have the right to download, and follow Yandex's terms of service and copyright law. You are responsible for how you use it.
- **How it works.** Open links use Yandex.Disk's documented public API. For **password-protected links** the app opens the link page in headless Chromium,
  signs in with the password you entered, and then calls **undocumented internal web endpoints**. That part is experimental, may violate Yandex's terms, and may stop working at any time.
  Requests carry a browser `User-Agent`.
- **No circumvention.** On 429/503 or a captcha the app pauses and waits; it does not solve captchas or evade limits.
- **No warranty** (MIT). Deleting in the file manager is irreversible; keep backups. The image bundles third-party components (Chromium via Playwright, Caddy) under their own licenses.
