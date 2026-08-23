# OS Update Agent

The OS Update Agent runs on the **host** (not in the container) and checks for available OS package updates. It writes the results to a JSON file that the docker-update-checker container can read.

This is the **recommended approach** for production deployments as it:
- Runs with appropriate host-level permissions
- Doesn't require root access in the container
- Supports all major Linux distributions
- Integrates with existing docker-update-checker notification system

## Quick Start

### Option 1: systemd (Recommended)

1. Copy the agent files to your host:
   ```bash
   sudo mkdir -p /opt/docker-update-checker/scripts
   sudo cp scripts/os_update_agent.py /opt/docker-update-checker/scripts/
   sudo cp scripts/os_update_agent.service /etc/systemd/system/
   sudo cp scripts/os_update_agent.timer /etc/systemd/system/
   ```

2. Reload systemd and enable the timer:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable os_update_agent.timer
   sudo systemctl start os_update_agent.timer
   ```

3. The agent will now run hourly and write results to `/var/lib/docker-update-checker/os-updates.json`

### Option 2: Cron

1. Copy the script to your host:
   ```bash
   sudo mkdir -p /opt/docker-update-checker/scripts
   sudo cp scripts/os_update_agent.py /opt/docker-update-checker/scripts/
   sudo cp scripts/os_update_agent.sh /opt/docker-update-checker/scripts/
   sudo chmod +x /opt/docker-update-checker/scripts/os_update_agent.sh
   ```

2. Add a cron job (runs hourly):
   ```bash
   # Edit root's crontab
   sudo crontab -e
   
   # Add this line
   0 * * * * /opt/docker-update-checker/scripts/os_update_agent.sh
   ```

### Option 3: Manual Run

```bash
sudo python3 /opt/docker-update-checker/scripts/os_update_agent.py
```

## Docker Container Configuration

Update your `compose.yaml` to mount the output file:

```yaml
services:
  update-checker:
    # ... existing configuration ...
    volumes:
      # Mount the OS updates file (read-only)
      - /var/lib/docker-update-checker/os-updates.json:/var/lib/docker-update-checker/os-updates.json:ro
```

Or if you placed the file elsewhere:

```yaml
volumes:
  - /custom/path/os-updates.json:/var/lib/docker-update-checker/os-updates.json:ro
```

## Environment Variables

You can customize the agent's behavior using environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_FILE` | `/var/lib/docker-update-checker/os-updates.json` | Path to output JSON file |
| `LOG_FILE` | `/var/log/docker-update-checker/os-updates.log` | Path to log file |

Example usage:
```bash
OUTPUT_FILE=/custom/path/os-updates.json LOG_FILE=/custom/path/log.log \
  python3 os_update_agent.py
```

## Output Format

The agent writes a JSON file with the following structure:

```json
{
  "os": "Ubuntu",
  "version": "22.04",
  "family": "debian",
  "package_manager": "apt",
  "updates_available": 12,
  "security_updates": 3,
  "packages": [
    {
      "name": "libssl3",
      "current": "3.0.2",
      "available": "3.0.7",
      "security": true
    },
    {
      "name": "openssh-server",
      "current": "8.9p1",
      "available": "9.0p1",
      "security": true
    }
  ],
  "last_checked": "2026-08-22T23:32:50.123456+00:00",
  "host": "my-server-01",
  "error": null,
  "source": "mounted_file"
}
```

## Supported Operating Systems

| OS | Package Manager | Notes |
|----|----------------|-------|
| Ubuntu | apt | Tested on 20.04, 22.04, 24.04 |
| Debian | apt | Tested on 10, 11, 12 |
| CentOS | dnf | Tested on 7, 8, 9 |
| RHEL | dnf | Tested on 8, 9 |
| Fedora | dnf | Tested on 36+ |
| Alpine | apk | Tested on 3.14+ |
| Arch Linux | pacman | Tested on current |

## Troubleshooting

### "apt-get not found" or similar errors

The agent requires the appropriate package manager to be installed. If you see this error:
- Ensure you're running on a supported OS
- Verify the package manager is installed: `which apt-get` or `which dnf`

### Permission denied errors

The agent needs to run as root to access package manager commands:
- Use `sudo` when running manually
- Ensure the systemd service runs as root
- Ensure the cron job runs as root

### File not found errors

Ensure the output directory exists:
```bash
sudo mkdir -p /var/lib/docker-update-checker /var/log/docker-update-checker
sudo chown root:root /var/lib/docker-update-checker /var/log/docker-update-checker
```

### Outdated information

The docker-update-checker caches the file for up to 1 hour. To force a refresh:
- Click the "Check" button in the OS Package Updates card
- Or touch the file on the host: `sudo touch /var/lib/docker-update-checker/os-updates.json`

## Security Considerations

- The agent runs as **root** on the host (required for package manager access)
- The output file is world-readable (644 permissions)
- The container mounts the file as **read-only**
- No network access is required from the container to the host
- The agent only runs package manager check commands, not installs

## Disabling OS Update Checks

To disable OS update checking entirely, set the environment variable in your compose file:

```yaml
environment:
  OS_UPDATE_CHECK_ENABLED: "false"
```

This will prevent the docker-update-checker from reading the file or attempting direct checks.
