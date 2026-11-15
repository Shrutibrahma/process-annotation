# Process Annotation on Oracle Cloud (free forever)

Oracle's **Always Free** tier includes an ARM server with up to 4 CPU cores, 24 GB memory and 200 GB
disk at no cost. You do steps 1-3 once in the Oracle console (about 20 minutes); step 4 installs and
updates the app from this laptop with one command.

## 1. Create the Oracle account

1. Go to <https://signup.cloud.oracle.com> and sign up for **Cloud Free Tier**.
2. A card is needed to verify you; Always Free resources are not charged.
3. **Home region**: pick the one closest to you. It cannot be changed later, and free servers
   only run in it.

## 2. Create the server

Console menu > **Compute** > **Instances** > **Create instance**.

| Setting | Choose |
|---|---|
| Name | `process-annotation` |
| Image | **Canonical Ubuntu 24.04** (Change image > Ubuntu) |
| Shape | Change shape > **Ampere** > **VM.Standard.A1.Flex**, **4 OCPUs, 24 GB memory** (shows "Always Free-eligible") |
| Networking | Create new virtual cloud network, **public subnet**, **Assign a public IPv4 address: Yes** |
| SSH keys | **Generate a key pair for me** > **Save private key** (keep the `.key` file, e.g. in Downloads) |
| Boot volume | Specify a custom size: **100 GB** (free up to 200 GB in total) |

Click **Create**. When it shows *Running*, copy the **Public IP address**.

If it says **Out of capacity** for A1: try another *Availability domain* on the same page, or try
again later, or use shape **VM.Standard.E2.1.Micro** (also Always Free; 1 GB memory, slower but works:
the setup adds swap).

## 3. Open the web ports

Instance page > **Subnet** link > **Security Lists** > **Default Security List** > **Add Ingress Rules**:

| Source CIDR | IP protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

(Port 80 is only used to get the free HTTPS certificate.)

## 4. Install the app (from this laptop)

In PowerShell, in the `work-study` folder:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\oracle\deploy.ps1 -Ip <PUBLIC IP> -Key <path to the .key file> -WithData
```

It installs Docker and the firewall rules on the server, sends the app and your settings (Gemini
key from `.env`, never printed), copies your studies (`-WithData`), starts it with automatic HTTPS
and prints:

```
Process Annotation is live:  https://129-146-12-34.sslip.io
Access code to create a team: pa-1234
```

Open the link, **Create account > Start a new team** with that access code. The first team gets
your copied studies. Colleagues use **Join my team** with the invite code in your sidebar.

**Updating later**: commit your changes, run the same command without `-WithData`. Data, accounts
and the access code stay.

## Costs

Server, disk and HTTPS: **$0** on Always Free. You only pay Google for Gemini per video analysed
(capped at 30 analyses per team per month, `WORKSTUDY_MONTHLY_ANALYSES` in `.env`).
Oracle may reclaim an Always Free server that sits almost idle for 7 days; using the app or
upgrading the account to Pay As You Go (still free for these resources) avoids it.
