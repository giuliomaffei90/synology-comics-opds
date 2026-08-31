# Comics OPDS

A small, self-hosted OPDS server for a folder of comic books.

It scans a directory tree, indexes it in SQLite, and serves an authenticated
OPDS 1.2 catalogue that any OPDS reader can browse. It streams files with HTTP
range support, extracts and caches covers, and exposes page-by-page reading so
a client can open an issue without downloading the whole archive first.

**No dependencies.** Python 3.8+ standard library only — nothing to compile,
nothing to `pip install`, which makes it a good fit for a low-powered ARM64 NAS
where Docker is not available.

---

## Features

- **Folder tree navigation** — your directory structure is the catalogue.
  Subfolders and comics appear together in one natural sort order
  (`#1`, `#2`, `#10`, `Artbook`), not folders-then-files.
- **Folder previews** — every folder shows the cover of its first comic, found
  recursively, so a folder that only contains subfolders still gets a thumbnail.
- **Real format detection** — the archive type comes from magic bytes, not the
  file extension. A `.cbr` that is actually a ZIP is served with the correct
  MIME type.
- **Page streaming** — every ZIP-based archive exposes an
  [OPDS-PSE](https://vaemendis.net/opds-pse/) link, so clients can fetch one
  page at a time instead of the whole file.
- **HTTP range requests** — partial downloads, resumable transfers, `HEAD`.
  Files are streamed in 64 KB chunks and never loaded into memory.
- **Cached covers** — extracted on first request, then served from disk.
- **Incremental scanning** — a file is only re-read when its path, size or
  mtime changes. Deleted files drop out of the index automatically.
- **HTTP Basic authentication** — passwords stored as pbkdf2 hashes.
- **Reverse proxy aware** — honours `X-Forwarded-Proto` / `X-Forwarded-Host`,
  or a configured base URL.
- **Your comics are never modified.** The server only ever reads them.

Supported formats:

| Extension / actual content | Download | Cover | Page streaming |
|---|---|---|---|
| ZIP (`.cbz`, `.zip`, and `.cbr` files that are really ZIP) | yes | yes | yes |
| Real RAR (`.cbr`, `.rar`) | yes | with `rarfile` | with `rarfile` |
| PDF | yes | no | no |
| EPUB | yes | no | no |
| `.cb7`, corrupted archives | yes | no | no |

---

## Requirements

- A Synology NAS running DSM 7 (or any Linux box — nothing here is
  Synology-specific except the setup instructions below)
- Python 3.8 or newer
- A folder full of comics

Optional: `pip3 install rarfile` plus an `unrar` binary, if you have real RAR
archives and want covers and page streaming for those too. Without it, RAR
files are still fully downloadable.

---

## Installation on Synology DSM 7

This guide assumes no prior command line experience. Every command is meant to
be copied and pasted exactly as written. It uses these paths throughout:

| | |
|---|---|
| Shared folder | `Contents` |
| Comics library | `/volume1/Contents/Comics` |
| This program | `/volume1/Contents/OpdsServer` |

If your shared folder has a different name, replace `Contents` with yours in
every command **and** in the config file later on.

### Step 1 — Find your NAS IP address

In DSM, open **Control Panel → Network → Network Interface**. Note the IPv4
address, something like `192.168.1.50`.

Everywhere below, replace `NAS_IP` with that address.

### Step 2 — Enable SSH

SSH is how you type commands on the NAS.

**Control Panel → Terminal & SNMP → Terminal tab → tick "Enable SSH service"
→ Apply.** Leave the port at `22`.

> SSH only accepts users in the **administrators** group. If your account is
> not one, either add it (Control Panel → User & Group) or use your admin
> account for the installation.

### Step 3 — Check whether you already have Python

Open **Terminal** on macOS/Linux, or **PowerShell** on Windows, and connect:

```bash
ssh YOUR_USER@NAS_IP
```

Type your DSM password (nothing appears as you type — that is normal), then:

```bash
python3 -V
```

- If it prints `Python 3.8` or higher, **you are done with this step.** DSM 7
  ships with Python 3.8 and that is enough.
- If it prints nothing, or a version below 3.8, install **Python 3.9** from
  **Package Center** in DSM, then run `python3 -V` again.

Now find the exact location of the interpreter — you will need it later:

```bash
which python3
```

Write down what it prints, for example `/bin/python3` or
`/usr/local/bin/python3`. Everywhere below, replace `PYTHON` with that path.

> If you installed the Package Center version, it may only be available as
> `/usr/local/bin/python3.9`. Check with `ls /usr/local/bin/python3*`.
> Prefer that one: it is a package you control, whereas `/bin/python3` belongs
> to DSM and could change with a system update.

### Step 4 — Download the program onto the NAS

Still connected over SSH:

```bash
cd /volume1/Contents
git clone https://github.com/giuliomaffei90/synology-comics-opds.git OpdsServer
```

**If `git` is not installed**, download the ZIP instead:

```bash
cd /volume1/Contents
wget -O opds.zip https://github.com/giuliomaffei90/synology-comics-opds/archive/refs/heads/main.zip
7z x opds.zip && mv synology-comics-opds-main OpdsServer && rm opds.zip
```

**If neither works**, download the ZIP on your computer from the green *Code*
button on GitHub, unzip it, rename the folder to `OpdsServer`, and drag it into
the `Contents` shared folder using **File Station** in DSM.

Check that it arrived:

```bash
ls /volume1/Contents/OpdsServer/app
```

You should see `library.py  opds.py  server.py`.

### Step 5 — Verify it runs on your NAS

Before configuring anything, run the built-in test suite. It builds a fake
library in a temporary folder, starts a server on a random port, and checks
everything works. **It does not touch your comics.**

```bash
PYTHON /volume1/Contents/OpdsServer/test_server.py
```

You want a list of `ok ...` lines ending with `All checks passed.` If it fails
here, stop and open an issue — no point continuing.

### Step 6 — Create your config file

```bash
cd /volume1/Contents/OpdsServer
cp config/config.example.yaml config/config.yaml
```

### Step 7 — Set a password

This is the password you will type into your OPDS reader. It has nothing to do
with your DSM password — pick a new one.

```bash
PYTHON /volume1/Contents/OpdsServer/app/server.py passwd
```

It asks for the password twice (invisible as you type) and prints a line like:

```
  password_hash: pbkdf2$120000$3f9a...$7c21...
```

**Select and copy that whole `pbkdf2$...` string.** The plain password is never
stored anywhere.

Now paste it into the config. Replace `PASTE_HERE` with what you copied, keeping
the single quotes around the whole command:

```bash
sed -i 's|^  password_hash:.*|  password_hash: PASTE_HERE|' /volume1/Contents/OpdsServer/config/config.yaml
```

> The single quotes matter: they stop the shell from interpreting the `$`
> characters in the hash.

### Step 8 — Check the config

```bash
cat /volume1/Contents/OpdsServer/config/config.yaml
```

Confirm three things:

1. `library: path:` points at your actual comics folder
2. `username:` is what you want to type in your reader — change it with
   `sed -i 's|^  username:.*|  username: YOUR_NAME|' /volume1/Contents/OpdsServer/config/config.yaml`
3. `password_hash:` contains the long `pbkdf2$...` string

### Step 9 — First scan

```bash
PYTHON /volume1/Contents/OpdsServer/app/server.py scan
```

This prints one line per comic found. The last line is the summary:

```
scan end in 32.1s: +822 ~0 -0 errors=0
```

`+` added · `~` updated · `-` removed · `errors` files that could not be read.

Only the first scan is slow. Later ones skip everything that has not changed.

### Step 10 — Start it and look at it

```bash
PYTHON /volume1/Contents/OpdsServer/app/server.py run
```

The terminal stays busy showing live logs. You should see:

```
server listening on 0.0.0.0:2202 (library /volume1/Contents/Comics, auth on)
```

Leave it running and open a browser on your computer:

```
http://NAS_IP:2202/
```

Enter your username and the password from step 7. You should get a status page
with folder and comic counts, the last scan time, and a *Scan now* button.

Press **Ctrl-C** in the terminal to stop it.

**If the browser times out** while the terminal shows nothing, the DSM firewall
is blocking the port: **Control Panel → Security → Firewall** → allow TCP
`2202` from your local network.

**If the port is already in use**, something else is on 2202. Change it:

```bash
sed -i 's|^  port: 2202|  port: 2210|' /volume1/Contents/OpdsServer/config/config.yaml
```

### Step 11 — Run it in the background

Now start it properly, detached from your terminal:

```bash
PYTHON /volume1/Contents/OpdsServer/app/server.py start
```

It prints `started (pid 12345)` and returns immediately. The server keeps
running after you close the SSH session.

```bash
PYTHON /volume1/Contents/OpdsServer/app/server.py status   # is it alive?
PYTHON /volume1/Contents/OpdsServer/app/server.py stop      # shut it down
PYTHON /volume1/Contents/OpdsServer/app/server.py restart   # after a config change
```

### Step 12 — Start automatically on boot

**Control Panel → Task Scheduler → Create → Triggered Task → User-defined script**

| Field | Value |
|---|---|
| Task | `Comics OPDS` |
| User | `root` |
| Event | **Boot-up** |
| Run command | `PYTHON /volume1/Contents/OpdsServer/app/server.py start` |

Click **OK**, confirm the warning, then select the task and click **Run** to
test it without rebooting.

Verify:

```bash
PYTHON /volume1/Contents/OpdsServer/app/server.py status
```

> The command returns immediately because the server detaches itself, so DSM
> marks the task complete and leaves it running. If it had stayed in the
> foreground, DSM would kill it.

### Step 13 — Add it to your reader

In your OPDS client, add a new catalogue:

| Field | Value |
|---|---|
| URL | `http://NAS_IP:2202/opds` |
| Username | from your config |
| Password | from step 7 |

Browse in, and you should see your folders. The first time you open a folder
the covers are extracted on the fly, so give it a moment; afterwards they come
from the cache.

Tested with [Panels](https://panels.app) on iOS. Any OPDS 1.x client should work.

---

## Remote access

The server does not do TLS. Put DSM's reverse proxy in front of it.

**Control Panel → Login Portal → Advanced → Reverse Proxy → Create**

| | |
|---|---|
| Source | `HTTPS` · `comics.yourdomain.tld` · port `443` |
| Destination | `HTTP` · `localhost` · port `2202` |

Then, in the same rule, open the **Custom Header** tab and add:

| Header name | Value |
|---|---|
| `X-Forwarded-Proto` | `https` |

Without it the links inside the catalogue come back as `http://` even over an
HTTPS connection, because DSM does not send that header by default.

**Alternative**, if custom headers are unavailable: force the public URL in
`config.yaml`.

```yaml
server:
  base_url: https://comics.yourdomain.tld
```

Be aware that `base_url` applies **always**, including on your LAN. Use it only
if you always reach the server through that hostname.

`GET /health` returns `ok` **without authentication** — use it as the proxy
health check.

Do not forward port 2202 directly from your router. Everything is plain HTTP.

---

## Commands

```bash
python3 app/server.py run       # foreground, logs to the terminal
python3 app/server.py start     # background daemon, writes data/server.pid
python3 app/server.py stop      # SIGTERM, then SIGKILL after 10s
python3 app/server.py restart
python3 app/server.py status
python3 app/server.py scan      # one-off scan, then exit
python3 app/server.py passwd    # generate a password hash
```

Every command takes an optional config path as its second argument:

```bash
python3 app/server.py start /path/to/other-config.yaml
```

---

## Configuration

```yaml
server:
  host: 0.0.0.0
  port: 2202
  base_url:            # empty = derive from X-Forwarded-* / Host

library:
  path: /volume1/Contents/Comics

database:
  path: /volume1/Contents/OpdsServer/data/library.db

cache:
  path: /volume1/Contents/OpdsServer/cache

security:
  enabled: true
  username: comics
  password_hash: pbkdf2$120000$...

scan:
  on_startup: true
  interval_minutes: 60     # 0 disables periodic scanning

logging:
  path: /volume1/Contents/OpdsServer/logs/server.log
  level: INFO              # DEBUG also logs every HTTP request
  max_bytes: 2000000
  backups: 3
```

Every value can be overridden by an environment variable named
`OPDS_<SECTION>_<KEY>`, for example `OPDS_SERVER_PORT=2210` or
`OPDS_LIBRARY_PATH=/mnt/comics`.

> The YAML parser is deliberately minimal: a section, then indented
> `key: value` pairs. No lists, anchors or multi-line strings. This keeps the
> project dependency-free instead of requiring PyYAML.

---

## Endpoints

| Method | Path | |
|---|---|---|
| GET | `/` | status page with a scan button |
| GET | `/opds` | root folder feed |
| GET | `/opds/folder/<id>` | one folder: subfolders and comics |
| GET | `/file/<id>` | the original file, supports `Range` |
| GET | `/cover/<id>` | cover image, extracted on first access then cached |
| GET | `/page/<id>?page=N` | a single page (OPDS-PSE), `N` starts at 0 |
| POST | `/admin/scan` | trigger a background scan |
| GET | `/health` | `ok`, **no authentication** |

Everything else requires HTTP Basic auth. All `<id>` values are hashes of the
relative path, so the real filesystem layout never appears in a URL, and
requests are resolved against the database rather than by joining user input
onto a path.

---

## How it works

**Scanning** walks the library, and for each file compares path, size and mtime
against the database. Unchanged files are skipped entirely. For archives it
reads the central directory — cheap, no decompression — to count pages and pick
up `ComicInfo.xml` if present. The folder tree is rebuilt from scratch at the
end of each scan, so it can never drift out of sync with the files. Scans run in
a background thread and do not block HTTP.

**Metadata** comes from `ComicInfo.xml` when available (`Title`, `Series`,
`Number`, `Volume`, `Year`, `Writer`, `Publisher`, `Summary`). Without it, the
folder name becomes the series and the filename becomes the title, with a
trailing number parsed out as the issue number.

**Covers** are extracted the first time they are requested, not during the scan.
That keeps scans fast and means comics you never open cost nothing. The first
valid image in natural sort order wins, ignoring `__MACOSX`, dotfiles,
`ComicInfo.xml` and `metadata.xml`. Images are cached verbatim — never
re-encoded.

**Page ordering** uses natural sort everywhere, so `1.jpg`, `2.jpg`, `10.jpg`
come back in that order rather than `1`, `10`, `2`.

**Logging** goes to `logs/server.log`, rotating at 2 MB with 3 backups: startup
and shutdown, scan summaries, every added, updated and removed file, unreadable
archives, failed logins. Passwords are never logged.

---

## Tests

```bash
python3 test_server.py
```

Builds a fake library in a temporary directory — including a `.cbr` that is
really a ZIP, a real RAR, a PDF, a corrupted archive and a nested folder tree —
then starts a server on an ephemeral port and asserts 22 behaviours end to end:
incremental scanning, tree navigation, mixed feeds, MIME types, authentication,
range requests, `206`/`416`, covers, page streaming, reverse proxy headers and
path traversal.

No test framework, no fixtures, no dependencies.

---

## Limitations

- Threaded `http.server`, sized for a household, not for dozens of concurrent
  clients.
- No search, no reading progress sync, no user accounts beyond the single
  configured login.
- No covers for PDF or EPUB — that would mean a heavy dependency to compile on
  ARM64.

## License

MIT
