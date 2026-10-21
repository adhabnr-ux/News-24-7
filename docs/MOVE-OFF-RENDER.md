# Moving News247 off Render

Render's free plan now includes **5 GB of outbound data a month** (it was 100 GB until 2026). When
a workspace uses it up, Render suspends its services until the next month unless a card is on
file. News247 sends a steady stream to an open app (it refreshes every minute, and the Watch tab
pulls about 40 boards each time), so 5 GB doesn't last a month.

There are three ways out. All of them run the same code with the same settings.

| Option | Cost | Data a month | Effort |
|---|---|---|---|
| **Stay on Render, add a card** | $0.15 per GB over 5 GB ($15 per 100 GB) | unlimited, metered | 2 minutes, nothing moves |
| **Hetzner Cloud** (smallest shared plan) | about €4 a month | 20 TB (EU locations) | about 15 minutes |
| **Oracle Cloud Always Free** | $0 | 10 TB | about 30 minutes; free machines are sometimes out of capacity |

Prices and allowances change; check each provider's pricing page before you sign up.

## What carries over

- **Phones keep their alerts if you keep `DASHBOARD_TOKEN`.** The push signing key is derived
  from it, so a new server with the same token can keep pushing to phones that subscribed on
  Render, as long as the list of subscribed phones carries over. It does when you used `STATE_DB`
  (a free Postgres such as Neon) on Render. Otherwise each phone opens the new address once and
  taps **Turn on alerts**.
- **History starts fresh**, as it did after every Render restart. From here on it's kept: unlike
  Render's free disk, a server's disk is permanent.
- **The address changes** (unless you bring your own domain). Open the app at the new address and
  add it to the home screen again.

## Moving to your own server (Hetzner, Oracle, or any Ubuntu server)

### 1. Get a server

- **Hetzner:** sign up at hetzner.com/cloud, create a project, then **Add Server**. Choose an EU
  location (for the 20 TB allowance), the **Ubuntu 24.04** image and the smallest shared-CPU plan.
  Add your SSH key, or use the root password Hetzner emails you.
- **Oracle Cloud:** sign up at oracle.com/cloud/free, then **Create a VM instance**. Choose the
  **Ubuntu** image and an Always Free shape: **VM.Standard.A1.Flex** (Arm) or
  **VM.Standard.E2.1.Micro**. If Arm machines are out of capacity, try another availability
  domain, or the E2 micro. In the instance's **subnet → security list**, add ingress rules for
  TCP **80** and **443** from `0.0.0.0/0`.

Copy the server's public IP address.

### 2. Run the setup

Connect to the server (`ssh root@YOUR-IP`, or `ssh ubuntu@YOUR-IP` on Oracle) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/adhabnr-ux/News-24-7/claude/market-monitoring-system-bq69x4/deploy/vps/setup.sh | sudo bash
```

It installs Docker, downloads the app and asks for:

- **`DASHBOARD_TOKEN`**: copy it from Render → your service → **Environment**. Keep the same one.
- **`CONTACT_EMAIL`**;
- **`STATE_DB`, `ALPACA_KEY`, `ALPACA_SECRET`** if you had them on Render (press Enter to skip);
- **a domain**: press Enter for a free address like `https://203-0-113-7.sslip.io`, or enter your
  own domain after pointing an A record at the server.

It then gets an HTTPS certificate, starts the app, opens ports 80 and 443 (including Oracle's
built-in firewall), and checks GitHub every hour for updates, as Render's auto-deploy did.

**WhatsApp:** if you used it, add the `WHATSAPP_*` values from Render to `/opt/news247/deploy/vps/.env`,
run `cd /opt/news247/deploy/vps && docker compose up -d`, and change the webhook URL in Meta's app
dashboard to the new address (the Setup page shows it).

### 3. Phones

Open `https://YOUR-ADDRESS/app/?token=YOUR_DASHBOARD_TOKEN`, **Add to Home Screen**, then tap
**Turn on alerts**. Remove the old Render icon.

### 4. Turn Render off

In Render, open the service, go to **Settings**, then **Suspend** (or **Delete**). Otherwise it
comes back when the 5 GB resets next month, and every alert arrives twice.

## Running it

```bash
cd /opt/news247/deploy/vps
docker compose logs -f news247   # live logs
docker compose restart news247   # restart
docker compose up -d --build     # rebuild now, without waiting for the hourly update
nano .env                        # change settings, then: docker compose up -d
```

Settings live in `/opt/news247/deploy/vps/.env` (never committed). Data lives in the
`news247-data` Docker volume, and survives restarts, updates and reboots.
