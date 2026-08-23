#!/usr/bin/env python3
"""
OS Update Agent for docker-update-checker

This script runs on the HOST (not in the container) and checks for available
OS package updates. It writes the results to a JSON file that the
docker-update-checker container can read.

Usage:
    # Run manually
    python3 os_update_agent.py
    
    # Or via systemd timer (see os_update_agent.service and os_update_agent.timer)
    
    # Or via cron (see os_update_agent.sh)

Configuration:
    - Output file: /var/lib/docker-update-checker/os-updates.json
    - Log file: /var/log/docker-update-checker/os-updates.log
    
Environment Variables:
    OUTPUT_FILE: Path to output JSON file (default: /var/lib/docker-update-checker/os-updates.json)
    LOG_FILE: Path to log file (default: /var/log/docker-update-checker/os-updates.log)
    CHECK_INTERVAL: How often to check in hours (default: 24, for manual runs)
"""

import os
import sys
import json
import logging
import subprocess
import datetime
import platform
from pathlib import Path
from typing import Dict, List, Optional


# Configuration
DEFAULT_OUTPUT_FILE = "/var/lib/docker-update-checker/os-updates.json"
DEFAULT_LOG_FILE = "/var/log/docker-update-checker/os-updates.log"
DEFAULT_CHECK_INTERVAL_HOURS = 24

# Ensure log directory exists
Path(DEFAULT_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.environ.get("LOG_FILE", DEFAULT_LOG_FILE)),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


def detect_os() -> Dict[str, str]:
    """Detect the host operating system."""
    try:
        with open("/etc/os-release", "r") as f:
            lines = f.readlines()
        
        os_info = {}
        for line in lines:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                os_info[key] = value.strip('"')
        
        name = os_info.get("NAME", "").lower()
        version = os_info.get("VERSION_ID", "").strip('"')
        id_like = os_info.get("ID_LIKE", "").lower()
        
        if "ubuntu" in name or "ubuntu" in id_like:
            return {"os": "Ubuntu", "version": version, "family": "debian", "package_manager": "apt"}
        elif "debian" in name or "debian" in id_like:
            return {"os": "Debian", "version": version, "family": "debian", "package_manager": "apt"}
        elif "centos" in name or "rhel" in name or "fedora" in name:
            return {"os": name.title(), "version": version, "family": "redhat", "package_manager": "dnf"}
        elif "alpine" in name:
            return {"os": "Alpine", "version": version, "family": "alpine", "package_manager": "apk"}
        elif "arch" in name:
            return {"os": "Arch", "version": version, "family": "arch", "package_manager": "pacman"}
        else:
            return {"os": name.title() if name else "Unknown", "version": version, "family": "unknown", "package_manager": "unknown"}
    except Exception as e:
        log.warning(f"Could not detect OS: {e}")
        return {"os": "Unknown", "version": "", "family": "unknown", "package_manager": "unknown"}


def check_apt_updates() -> List[Dict]:
    """Check for updates using apt (Debian/Ubuntu)."""
    packages = []
    try:
        # Update package lists first
        subprocess.run(["apt-get", "update", "-qq"], timeout=60, check=False)
        
        # List upgradable packages
        result = subprocess.run(
            ["apt-get", "-qq", "list", "--upgradable", "2>/dev/null"],
            timeout=30,
            capture_output=True,
            text=True
        )
        
        for line in result.stdout.strip().split('\n'):
            if line and not line.startswith("Listing"):
                parts = line.split()
                if len(parts) >= 2:
                    pkg_name = parts[0].split('/')[0]
                    versions = parts[1].split('/')
                    current = versions[0] if len(versions) > 0 else "unknown"
                    available = versions[1] if len(versions) > 1 else "unknown"
                    is_security = "security" in line.lower()
                    
                    packages.append({
                        "name": pkg_name,
                        "current": current,
                        "available": available,
                        "security": is_security
                    })
                    
    except subprocess.TimeoutExpired:
        log.warning("apt-get check timed out")
    except FileNotFoundError:
        log.warning("apt-get not found")
    except Exception as e:
        log.warning(f"apt check failed: {e}")
    
    return packages


def check_dnf_updates() -> List[Dict]:
    """Check for updates using dnf (RHEL/CentOS/Fedora)."""
    packages = []
    try:
        result = subprocess.run(
            ["dnf", "check-update", "-q"],
            timeout=30,
            capture_output=True,
            text=True
        )
        
        for line in result.stdout.strip().split('\n'):
            if line and not line.startswith("Last metadata") and '.' in line:
                parts = line.split()
                if len(parts) >= 3:
                    pkg_info = parts[0]
                    pkg_name = pkg_info.split('.')[0]
                    version_info = parts[1]
                    versions = version_info.split('->')
                    current = versions[0] if len(versions) > 0 else "unknown"
                    available = versions[1] if len(versions) > 1 else "unknown"
                    
                    packages.append({
                        "name": pkg_name,
                        "current": current,
                        "available": available,
                        "security": "security" in line.lower() or "update" in line.lower()
                    })
                    
    except subprocess.TimeoutExpired:
        log.warning("dnf check timed out")
    except FileNotFoundError:
        log.warning("dnf not found")
    except Exception as e:
        log.warning(f"dnf check failed: {e}")
    
    return packages


def check_apk_updates() -> List[Dict]:
    """Check for updates using apk (Alpine)."""
    packages = []
    try:
        # Update first
        subprocess.run(["apk", "update"], timeout=60, check=False)
        
        result = subprocess.run(
            ["apk", "list", "--upgradable"],
            timeout=30,
            capture_output=True,
            text=True
        )
        
        for line in result.stdout.strip().split('\n'):
            if line and len(line.split()) >= 2:
                parts = line.split()
                pkg_name = parts[0]
                # Version might be in format: current-r0 -> available-r1
                version_info = parts[1] if len(parts) > 1 else ""
                versions = version_info.split("->")
                current = versions[0].strip().split('-')[0] if versions else "unknown"
                available = versions[1].strip().split('-')[0] if len(versions) > 1 else "unknown"
                
                packages.append({
                    "name": pkg_name,
                    "current": current,
                    "available": available,
                    "security": "security" in line.lower()
                })
                    
    except subprocess.TimeoutExpired:
        log.warning("apk check timed out")
    except FileNotFoundError:
        log.warning("apk not found")
    except Exception as e:
        log.warning(f"apk check failed: {e}")
    
    return packages


def check_pacman_updates() -> List[Dict]:
    """Check for updates using pacman (Arch)."""
    packages = []
    try:
        result = subprocess.run(
            ["pacman", "-Qu"],
            timeout=30,
            capture_output=True,
            text=True
        )
        
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 3 and "->" in parts[0]:
                    versions = parts[0].split("->")
                    current = versions[0].strip()
                    available = versions[1].strip()
                    pkg_name = parts[2]
                    
                    packages.append({
                        "name": pkg_name,
                        "current": current,
                        "available": available,
                        "security": False
                    })
                    
    except subprocess.TimeoutExpired:
        log.warning("pacman check timed out")
    except FileNotFoundError:
        log.warning("pacman not found")
    except Exception as e:
        log.warning(f"pacman check failed: {e}")
    
    return packages


def check_updates() -> Dict:
    """Check for OS package updates based on detected OS."""
    os_info = detect_os()
    package_manager = os_info.get("package_manager")
    
    result = {
        "os": os_info.get("os", "Unknown"),
        "version": os_info.get("version", ""),
        "family": os_info.get("family", "unknown"),
        "package_manager": package_manager,
        "updates_available": 0,
        "security_updates": 0,
        "packages": [],
        "last_checked": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": platform.node(),
        "error": None
    }
    
    if not package_manager or package_manager == "unknown":
        result["error"] = "Unsupported OS or no package manager detected"
        return result
    
    try:
        if package_manager == "apt":
            packages = check_apt_updates()
        elif package_manager == "dnf":
            packages = check_dnf_updates()
        elif package_manager == "apk":
            packages = check_apk_updates()
        elif package_manager == "pacman":
            packages = check_pacman_updates()
        else:
            result["error"] = f"Unsupported package manager: {package_manager}"
            return result
        
        result["updates_available"] = len(packages)
        result["security_updates"] = len([p for p in packages if p.get("security")])
        result["packages"] = packages
        
    except Exception as e:
        result["error"] = str(e)
    
    return result


def write_output(data: Dict, output_path: str) -> bool:
    """Write update data to JSON file."""
    try:
        # Ensure directory exists
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=2)
        
        # Set appropriate permissions
        os.chmod(output_path, 0o644)
        return True
    except Exception as e:
        log.error(f"Failed to write output file: {e}")
        return False


def main():
    """Main entry point."""
    output_file = os.environ.get("OUTPUT_FILE", DEFAULT_OUTPUT_FILE)
    
    log.info("Starting OS update check...")
    log.info(f"Output file: {output_file}")
    
    try:
        data = check_updates()
        
        if data.get("error"):
            log.warning(f"Update check completed with error: {data['error']}")
        else:
            updates = data.get("updates_available", 0)
            security = data.get("security_updates", 0)
            log.info(f"Found {updates} updates ({security} security) for {data.get('os', 'Unknown')}")
        
        # Write results to file
        if write_output(data, output_file):
            log.info(f"Results written to {output_file}")
        else:
            log.error("Failed to write results")
            sys.exit(1)
            
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
