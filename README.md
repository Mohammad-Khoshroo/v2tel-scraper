# v2tel-scraper

A simple Python script that fetches the latest messages from a list of public
Telegram channels and saves them to a text file.

Works natively on **Windows**, natively on **Linux**, inside **WSL2**,
or on a remote **server / VPS**.

---

## 1. Prerequisites

- Python 3.8+
- A Telegram account
- Telegram API credentials (`api_id` and `api_hash`)

### Getting API credentials

1. Go to <https://my.telegram.org>
2. Log in with your phone number
3. Open **API development tools**
4. Create an application (platform choice does not matter)
5. Copy your `api_id` (number) and `api_hash` (string)

> Each Telegram account can create **only one** application, but you can use
> those credentials in unlimited scripts.

---

## 2. Installation

### Windows (native)

1. Install Python from <https://www.python.org/downloads/>
   - **Important:** check ☑ "Add Python to PATH" during installation.
2. Open **Command Prompt** or **PowerShell**:

```bat
pip install telethon pysocks
```

### WSL2 / Linux

```bash
pip3 install telethon pysocks
```

---

## 3. Proxy Configuration (running from Iran?)

Telegram is blocked in Iran, so the script must connect through a proxy.
This section assumes a local **SOCKS5** proxy (v2rayN, v2rayA, Nekoray,
clash, etc.).

Pick the subsection matching **where the script runs** and **where the
proxy runs**:

| Your setup | Use |
|------------|-----|
| Script + proxy on the same Windows machine | Case 1 |
| Script in WSL2 (mirrored mode) | Case 2 |
| Script in WSL2 (NAT mode) | Case 3 |
| Script on Linux, proxy on the **same** Linux machine | Case 4a |
| Script on Linux, proxy on **another** machine | Case 4b |
| Script on a VPS **outside** Iran | No proxy needed |

**Rule of thumb:** if the script and the proxy run on the *same machine*,
use `127.0.0.1`. If they run on *different machines*, use the proxy
machine's IP and make sure the proxy accepts LAN connections.

---

### Case 1 — Running natively on Windows

The script and v2rayN run on the same system, so `127.0.0.1` works directly.

**1.** Make sure v2rayN is running and connected.

**2.** In the script, set:

```python
USE_PROXY = True
PROXY_HOST = '127.0.0.1'
PROXY_PORT = 10808
```

**3.** Run it:

```bat
python scraper.py
```

---

### Case 2 — WSL2 Mirrored Mode (recommended for WSL)

In mirrored mode, WSL shares the Windows `localhost`, so the Windows proxy is
directly reachable at `127.0.0.1`.

**1.** Make sure `C:\Users\<You>\.wslconfig` contains:

```ini
[wsl2]
networkingMode=mirrored
hostAddressLoopback=true
```

**2.** Restart WSL (in PowerShell):

```powershell
wsl --shutdown
```

**3.** Verify your mode from inside WSL:

```bash
wslinfo --networking-mode
# must print: mirrored
```

**4.** Verify the proxy is reachable from WSL:

```bash
curl -x socks5h://127.0.0.1:10808 -I https://t.me --max-time 10
```

If you get an HTTP response (`HTTP/2 200` or `HTTP/2 302`), you are good.

**5.** In the script, set:

```python
PROXY_HOST = '127.0.0.1'
PROXY_PORT = 10808
```

---

### Case 3 — WSL2 NAT Mode (default)

In NAT mode, WSL is a separate virtual machine. The proxy runs on the Windows
**host**, so you must use the host's IP and allow LAN connections.

**1.** In v2rayN enable:

```
Settings → Options → ☑ Allow connections from LAN
```

Then restart v2rayN.

**2.** Find the Windows host IP from inside WSL:

```bash
cat /etc/resolv.conf | grep nameserver | awk '{print $2}'
```

Example output: `172.25.80.1`

**3.** In the script, set:

```python
PROXY_HOST = '172.25.80.1'   # your actual Windows host IP
PROXY_PORT = 10808
```

**4.** If it still times out, allow port 10808 through the Windows Firewall
(create an Inbound Rule for TCP 10808).

---

### Case 4 — Running natively on Linux

#### Case 4a — Proxy on the same Linux machine

This behaves like Case 1: script and proxy share `localhost`.

Common proxy tools on Linux and their **default SOCKS ports**:

| Tool | Default SOCKS5 port |
|------|--------------------|
| v2rayA | `20170` |
| Nekoray / NekoBox | `2080` |
| clash / mihomo | `7890` (mixed) |
| xray / v2ray (manual config) | whatever you configured |

**1.** Make sure your proxy tool is running and connected.

**2.** Verify the proxy:

```bash
curl -x socks5h://127.0.0.1:20170 -I https://t.me --max-time 10
# replace 20170 with your tool's actual port
```

**3.** In the script, set:

```python
USE_PROXY = True
PROXY_HOST = '127.0.0.1'
PROXY_PORT = 20170   # your tool's SOCKS port
```

**4.** Run it:

```bash
python3 scraper.py
```

#### Case 4b — Proxy on another machine (LAN / another PC)

Same logic as Case 3, but between two Linux machines:

**1.** On the machine running the proxy, make it listen on all interfaces
(not just `127.0.0.1`) — e.g. enable "Allow LAN" in v2rayA/Nekoray settings,
or set the inbound listen address to `0.0.0.0`.

**2.** Find that machine's LAN IP (on the proxy machine):

```bash
ip addr | grep "inet " | grep -v 127.0.0.1
```

Example: `192.168.1.10`

**3.** In the script, set:

```python
PROXY_HOST = '192.168.1.10'   # the proxy machine's LAN IP
PROXY_PORT = 20170            # its SOCKS port
```

#### Case 4c — Remote server / VPS outside Iran

If your server can reach Telegram directly, disable the proxy entirely:

```python
USE_PROXY = False
```

---

### No proxy needed?

If you are on a network with direct access to Telegram (no censorship),
simply disable the proxy:

```python
USE_PROXY = False
```

---

## 4. Running

```bash
# Windows
python scraper.py

# WSL / Linux
python3 scraper.py
```

### First run — login

The first run asks for:

| Prompt | What to enter |
|--------|---------------|
| Phone number | International format, e.g. `+98912xxxxxxx` |
| Confirmation code | Sent to your **Telegram app** (chat with "Telegram"), not SMS |
| 2FA password | Only if you have one enabled |

After login, a `scraper_session.session` file is created. Subsequent runs
**skip the login entirely**.

> Keep the `.session` file private — it grants full access to your account.

### Output

All messages are written to `public_messages.txt` next to the script.

---

## 5. Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `ConnectionRefusedError` | Proxy not listening / LAN blocked | Enable "Allow LAN", restart the proxy tool |
| Timeout | Wrong `PROXY_HOST` or port | Verify with `curl -x socks5h://...` first |
| Timeout in NAT mode / Case 4b | Firewall | Allow the SOCKS port (TCP) through the firewall |
| `python` not recognized (Windows) | Python not in PATH | Reinstall Python with "Add to PATH" checked |
| `FloodWait` in output | Telegram rate limit | Script sleeps automatically and continues; lower `MESSAGE_LIMIT` if frequent |
| `Could not find the input entity` | Channel deleted/renamed | Ignore — script continues with remaining channels |

---

## 6. Notes

- `MESSAGE_LIMIT = 1000` over ~75 channels is heavy; start with a small
  value (e.g. `10`) for testing.
- Reading public channels does **not** require joining them.
- Channels that no longer exist are skipped gracefully.
- The script works identically everywhere — only `PROXY_HOST`,
  `PROXY_PORT` and `USE_PROXY` change depending on your setup
  (see section 3).
