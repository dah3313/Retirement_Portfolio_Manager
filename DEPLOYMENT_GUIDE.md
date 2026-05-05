# System Requirements & Deployment Guide
**Project:** Retirement Portfolio Manager (RPM)
**Operating Environment:** Interactive Brokers (IBKR)

### ⚠️ Critical Tax & Account Disclaimer ⚠️

This system is explicitly designed to operate within a tax-advantaged Roth or Traditional IRA]. The RPM's automated reallocation, rebalancing, and buffer refill mechanics rely on frictionless, tax-free internal trading. If this codebase is deployed in a standard taxable brokerage account, the automated drift-correction and cash-raising logic will generate a nightmare of frequent short-term capital gains and complex tax liabilities. Tax implications are entirely ignored by this software.

---

## Phase 1: Hardware Acquisition
The RPM is a stateless appliance[cite: 34]. It must be physically robust, draw minimal power, and recover instantly from outages. 

* **Primary Host:** Look for an industrial fanless mini-PC with an Intel Alder Lake N100 or N150 processor.
* **Chassis:** A metal chassis acts as a giant heatsink[cite: 6]. No fans mean no bearings to fail over a 20-year lifespan.
* **Memory & Storage:** 8GB of RAM is the absolute minimum, but 16GB is highly recommended to support the Java-based IB Gateway. Use a 128GB or 256GB NVMe/SATA SSD.
* **Network:** Hardwired Ethernet is non-negotiable. Do not rely on Wi-Fi for an automated trading appliance.
* **Power Protection:** A dedicated Uninterruptible Power Supply (UPS) (e.g., APC or CyberPower 600VA+) is critical. Connect your internet router and the RPM box to the battery backup side to ensure trades settle during brief power flickers.

---

## Phase 2: The BIOS Lockdown
Before installing an operating system, configure the motherboard to act like an appliance. Plug in a monitor and keyboard, boot the PC, and press the DEL or F2 key to enter the BIOS.

* **Restore on AC/Power Loss:** Change this setting to "Power On" or "Always On". If the house loses power long enough for the UPS battery to die, the RPM host must automatically turn itself back on the second the grid comes back online. It cannot wait for someone to press the physical power button.
* **Disable Wi-Fi/Bluetooth:** Disable the onboard wireless radios if the BIOS allows it. This prevents the OS from ever trying to connect to a less stable network.
* **Boot Order:** Ensure the primary NVMe drive is set as Option #1, and disable "Boot from USB" once the OS is installed.

---

## Phase 3: Operating System & Security
Ubuntu Server 26.04 LTS (or 24.04 LTS) is highly recommended. It installs zero bloated desktop software, uses less RAM, and has fewer background processes that could crash. Python 3.10 or higher is required.

* **The User Account:** Create a dedicated, non-root service account named `RPM_user` to run the application. Do not use "admin" or your own name.
* **OpenSSH:** Check the box to install the OpenSSH server during setup. This allows you to unplug the monitor and keyboard forever, place the box in its permanent home, and manage it remotely.
* **Static IP:** Configure the network interface to use a Static IP address rather than DHCP. This ensures your router always assigns the RPM box the exact same local IP (e.g., `192.168.1.50`).
* **Directory Structure:** Create `/home/RPM_user/RPM/` for the Python scripts. Create `/var/log/RPM/` for application logs, ensuring it has write permissions for `RPM_user`.
* **Alert Credentials:** Never hardcode credentials into your Python scripts. Create `.env` to securely store your email and SMS routing details. 

Create the `.env` file:
`nano /home/RPM_user/RPM/.env`

ini
# /home/RPM_user/RPM/.env
# Credentials for RPM AlertManager

# --- Email Routing ---
RPM_SMTP_SERVER=smtp.gmail.com
RPM_SMTP_PORT=587
RPM_EMAIL_SENDER=your_appliance_email@gmail.com
RPM_EMAIL_PASSWORD=your_16_digit_app_password
RPM_EMAIL_RECIPIENT=your_personal_email@gmail.com

# --- SMS Routing (Optional) ---
RPM_SMS_GATEWAY=5551234567@vtext.com

*Note: If using Gmail, you must use a 16-digit App Password, not your standard account password.*

Lock the file permissions immediately:
`chmod 600 /home/RPM_user/RPM/.env` 

---

## Phase 4: Interactive Brokers Gateway (IBC) Setup
The RPM connects to a local gateway running on the mini-PC, not directly to IBKR's cloud. You must install IB Gateway alongside IBC (IBController), an open-source wrapper that handles mandatory daily restarts and bypasses GUI prompts.

* **Step 1: The IBKR Web Portal Prerequisite**
  * Log into the IBKR website using your primary account. 
  * Navigate to Settings > Account Users.
  * Create a Secondary User specifically for API access.
  * Configure this secondary user to bypass 2FA. You cannot use your primary login, as the 2FA prompts will freeze the unattended appliance

* **Step 2: The IBC Configuration File**
Create the config file at `/home/RPM_user/ibc/config.ini`:

# /home/RPM_user/ibc/config.ini

IbLoginId=YOUR_SECONDARY_USERNAME
IbPassword=YOUR_SECONDARY_PASSWORD
TradingMode=live
IbDir=/home/RPM_user/Jts

StoreSettingsOnServer=no
MinimizeMainWindow=yes
ExistingSessionDetectedAction=primary
AcceptIncomingConnectionAction=accept

IbAutoClosedown=no
ClosedMktRestartTime=23:30

CommandServerPort=7462
ReadOnlyApi=no

Lock the file permissions immediately:
`chmod 600 /home/RPM_user/ibc/config.ini` 

---

## Phase 5: Remote Software Deployment
At this point, your mini-PC should be powered on, plugged into the router, and running Ubuntu. You no longer need a monitor or keyboard plugged into the appliance. You will do everything else remotely from your personal laptop.

* **Step 1:** Open the Command Prompt (Windows) or Terminal (Mac).
* **Step 2:** SSH into the Appliance using your assigned Static IP.
`ssh RPM_user@192.168.1.50` 
* **Step 3:** Accept the trust host warning by typing `yes`. Enter your password (typing will be invisible due to Linux security features).
* **Step 4:** Download the RPM Codebase.
`git clone https://github.com/DAH3313/rpm.git` 
`cd rpm` 
* **Step 5:** Install Python Dependencies. Ubuntu uses virtual environments to keep packages from interfering with the OS.
`python3 -m venv venv` 
`source venv/bin/activate` 
`pip install -r requirements.txt` 

---

## Phase 6: Systemd Automation Files
Do not use `cron`. Use `systemd` timers for superior logging and crash prevention. Create the following files in the `/etc/systemd/system/` directory using `sudo`.

**A. The Weekly Rebalancer (Mondays)**
`/etc/systemd/system/RPM-weekly.service` 
 
[Unit]
Description=RPM Weekly Drift & Rebalance Evaluation
After=network.target

[Service]
Type=oneshot
User=RPM_user
WorkingDirectory=/home/RPM_user/RPM
ExecStart=/usr/bin/python3 main.py --weekly
EnvironmentFile=/home/RPM_user/RPM/.env

[Install]
WantedBy=multi-user.target

`/etc/systemd/system/RPM-weekly.timer`
ini
[Unit]
Description=Timer for RPM Weekly Rebalance

[Timer]
OnCalendar=tz=America/New_York Mon *-*-* 09:30:00
Persistent=true

[Install]
WantedBy=timers.target

**B. The Monthly Cash Engine (Payday)**
`/etc/systemd/system/RPM-monthly.service` 
ini
[Unit]
Description=RPM Monthly Cash Raising & November Review
After=network.target

[Service]
Type=oneshot
User=RPM_user
WorkingDirectory=/home/RPM_user/RPM
ExecStart=/usr/bin/python3 main.py --monthly
EnvironmentFile=/home/RPM_user/RPM/.env

[Install]
WantedBy=multi-user.target

`/etc/systemd/system/RPM-monthly.timer` 
ini
[Unit]
Description=Timer for RPM Monthly Withdrawal

[Timer]
# Adjust the '11' to 3-4 business days prior to your ACH pull.
OnCalendar=tz=America/New_York *-*-11 09:30:00
Persistent=true

[Install]
WantedBy=timers.target


**C. The Hardware Sentinel (Weekly Heartbeat)**
`/etc/systemd/system/RPM-heartbeat.service` [cite: 58]
ini
[Unit]
Description=RPM Hardware & Network Sentinel Heartbeat
After=network.target

[Service]
Type=oneshot
User=RPM_user
WorkingDirectory=/home/RPM_user/RPM
ExecStart=/usr/bin/python3 main.py --heartbeat
EnvironmentFile=/home/RPM_user/RPM/.env

[Install]
WantedBy=multi-user.target

`/etc/systemd/system/RPM-heartbeat.timer` 
 
[Unit]
Description=Timer for RPM Heartbeat (Weekly)

[Timer]
OnCalendar=Sun *-*-* 12:00:00
Persistent=true

[Install]
WantedBy=timers.target


**D. The IBC Gateway Runner**

`/etc/systemd/system/ibgateway.service` 
 
[Unit]
Description=IB Gateway Headless Runner (IBC)
After=network.target

[Service]
Type=simple
User=RPM_user
WorkingDirectory=/home/RPM_user/ibc

ExecStart=/home/RPM_user/ibc/gatewaystart.sh -conf /home/RPM_user/ibc/config.ini -inline
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target

---

## Phase 7: Day 1 Initialization & Arming
Ensure the RPM mini-PC is powered on, connected to Ethernet, and the IBKR Gateway is running in the background. 

* **Step 1:** Run the initial setup script: `python3 setup.py`.
* **Step 2:** The script will connect to Interactive Brokers and present a list of legacy assets. Press `L` to Liquidate or `K` to Keep for each one.
* **Step 3:** Review the final blueprint of required targets. 
* **Step 4:** Confirm with `y`. The RPM will automatically execute live market orders to sell legacy assets, purchase the foundation ETFs, and write the permanent `rpm_state.json` file. Verify you receive the Day 1 Activation email alert.
* **Step 5:** Once the screen says "SETUP COMPLETE," run these commands to lock the schedules into place and arm the appliance:

`sudo systemctl daemon-reload`
`sudo systemctl enable RPM-weekly.timer`
`sudo systemctl enable RPM-monthly.timer`
`sudo systemctl enable RPM-heartbeat.timer`
`sudo systemctl enable ibgateway.service`

`sudo systemctl start ibgateway.service`
`sudo systemctl start RPM-weekly.timer`
`sudo systemctl start RPM-monthly.timer`
`sudo systemctl start RPM-heartbeat.timer`

* **Step 6:** Verify that all schedules are locked in by typing `systemctl list-timers | grep RPM. Check that the IBKR Gateway successfully connected by typing `systemctl status ibgateway.service`. 

If the Gateway is active, it provides a permanent, silent local port for the RPM to query balances and execute trades. No further human interaction is required for the lifespan of the hardware. You can close your terminal window; the RPM will now manage the portfolio autonomously.