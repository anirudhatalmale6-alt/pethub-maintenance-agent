import re
import ssl
import socket
import logging
from datetime import datetime, timezone

import httpx

from config import settings

logger = logging.getLogger("maintenance-agent.security")


def _check_ssl(hostname: str) -> dict:
    """Check SSL certificate details."""
    result = {"valid": False, "expiry": None, "issuer": None, "days_remaining": 0, "error": None}
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=hostname) as s:
            s.settimeout(10)
            s.connect((hostname, 443))
            cert = s.getpeercert()

        not_after = cert.get("notAfter", "")
        if not_after:
            expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            result["expiry"] = expiry.isoformat()
            result["days_remaining"] = (expiry - datetime.now(timezone.utc)).days
            result["valid"] = result["days_remaining"] > 0

        issuer_dict = dict(x[0] for x in cert.get("issuer", []))
        result["issuer"] = issuer_dict.get("organizationName", issuer_dict.get("commonName", "Unknown"))
    except Exception as e:
        result["error"] = str(e)[:200]

    return result


async def run_security_scan() -> dict:
    """Run comprehensive security scan."""
    started = datetime.now(timezone.utc)
    from urllib.parse import urlparse
    hostname = urlparse(settings.WP_URL).hostname
    findings = []
    severity_counts = {"critical": 0, "warning": 0, "info": 0, "pass": 0}

    # 1. SSL Certificate
    ssl_info = _check_ssl(hostname)
    if ssl_info["valid"]:
        if ssl_info["days_remaining"] < 30:
            findings.append({
                "check": "SSL Certificate",
                "status": "warning",
                "message": f"SSL expires in {ssl_info['days_remaining']} days (issuer: {ssl_info['issuer']})",
                "details": ssl_info,
            })
            severity_counts["warning"] += 1
        else:
            findings.append({
                "check": "SSL Certificate",
                "status": "pass",
                "message": f"Valid, expires in {ssl_info['days_remaining']} days (issuer: {ssl_info['issuer']})",
                "details": ssl_info,
            })
            severity_counts["pass"] += 1
    else:
        findings.append({
            "check": "SSL Certificate",
            "status": "critical",
            "message": f"SSL issue: {ssl_info.get('error', 'Invalid/expired')}",
            "details": ssl_info,
        })
        severity_counts["critical"] += 1

    async with httpx.AsyncClient(
        headers={"User-Agent": "PetHubMaintenanceBot/1.0"},
        verify=False,
    ) as client:
        # 2. Security Headers
        try:
            resp = await client.get(settings.WP_URL, timeout=30, follow_redirects=True)
            headers = resp.headers

            security_headers = {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": ["DENY", "SAMEORIGIN"],
                "Content-Security-Policy": None,
                "Strict-Transport-Security": None,
            }

            for header, expected in security_headers.items():
                val = headers.get(header, "")
                if val:
                    findings.append({
                        "check": f"Header: {header}",
                        "status": "pass",
                        "message": f"Present: {val[:100]}",
                    })
                    severity_counts["pass"] += 1
                else:
                    findings.append({
                        "check": f"Header: {header}",
                        "status": "warning",
                        "message": f"Missing security header: {header}",
                    })
                    severity_counts["warning"] += 1

            # 3. Mixed content check
            html = resp.text
            http_resources = re.findall(r'(?:src|href)=["\']http://[^"\']+', html, re.I)
            if http_resources:
                findings.append({
                    "check": "Mixed Content",
                    "status": "warning",
                    "message": f"Found {len(http_resources)} HTTP resources on HTTPS page",
                    "details": {"examples": http_resources[:5]},
                })
                severity_counts["warning"] += 1
            else:
                findings.append({
                    "check": "Mixed Content",
                    "status": "pass",
                    "message": "No mixed content detected",
                })
                severity_counts["pass"] += 1

        except Exception as e:
            findings.append({
                "check": "Security Headers",
                "status": "warning",
                "message": f"Could not fetch homepage: {e}",
            })
            severity_counts["warning"] += 1

        # 4. WP REST API exposure
        try:
            resp = await client.get(f"{settings.WP_URL}/wp-json/wp/v2/users", timeout=15)
            if resp.status_code == 200:
                users = resp.json()
                if users:
                    findings.append({
                        "check": "REST API User Enumeration",
                        "status": "warning",
                        "message": f"User enumeration possible - {len(users)} users exposed via REST API",
                    })
                    severity_counts["warning"] += 1
                else:
                    findings.append({
                        "check": "REST API User Enumeration",
                        "status": "pass",
                        "message": "Users endpoint returns empty",
                    })
                    severity_counts["pass"] += 1
            else:
                findings.append({
                    "check": "REST API User Enumeration",
                    "status": "pass",
                    "message": f"Users endpoint returned HTTP {resp.status_code}",
                })
                severity_counts["pass"] += 1
        except Exception:
            findings.append({"check": "REST API User Enumeration", "status": "info", "message": "Could not check"})
            severity_counts["info"] += 1

        # 5. Directory listing
        test_paths = ["/wp-content/uploads/", "/wp-includes/"]
        for path in test_paths:
            try:
                resp = await client.get(f"{settings.WP_URL}{path}", timeout=15)
                if resp.status_code == 200 and "Index of" in resp.text:
                    findings.append({
                        "check": f"Directory Listing: {path}",
                        "status": "warning",
                        "message": f"Directory listing enabled at {path}",
                    })
                    severity_counts["warning"] += 1
                else:
                    findings.append({
                        "check": f"Directory Listing: {path}",
                        "status": "pass",
                        "message": f"Directory listing disabled at {path}",
                    })
                    severity_counts["pass"] += 1
            except Exception:
                findings.append({"check": f"Directory Listing: {path}", "status": "info", "message": "Could not check"})
                severity_counts["info"] += 1

        # 6. robots.txt
        try:
            resp = await client.get(f"{settings.WP_URL}/robots.txt", timeout=15)
            if resp.status_code == 200:
                robots = resp.text
                sensitive_paths = ["/wp-admin", "/wp-login", "/wp-config"]
                exposed = [p for p in sensitive_paths if f"Allow: {p}" in robots]
                if exposed:
                    findings.append({
                        "check": "robots.txt",
                        "status": "warning",
                        "message": f"Sensitive paths allowed in robots.txt: {', '.join(exposed)}",
                    })
                    severity_counts["warning"] += 1
                else:
                    findings.append({
                        "check": "robots.txt",
                        "status": "pass",
                        "message": "robots.txt looks reasonable",
                    })
                    severity_counts["pass"] += 1
            else:
                findings.append({
                    "check": "robots.txt",
                    "status": "info",
                    "message": "No robots.txt found",
                })
                severity_counts["info"] += 1
        except Exception:
            pass

        # 7. xmlrpc.php
        try:
            resp = await client.post(
                f"{settings.WP_URL}/xmlrpc.php",
                content="<?xml version='1.0'?><methodCall><methodName>system.listMethods</methodName></methodCall>",
                headers={"Content-Type": "text/xml"},
                timeout=15,
            )
            if resp.status_code == 200 and "methodResponse" in resp.text:
                findings.append({
                    "check": "XML-RPC",
                    "status": "warning",
                    "message": "xmlrpc.php is accessible and responding - potential brute force attack vector",
                })
                severity_counts["warning"] += 1
            else:
                findings.append({
                    "check": "XML-RPC",
                    "status": "pass",
                    "message": "xmlrpc.php is blocked or disabled",
                })
                severity_counts["pass"] += 1
        except Exception:
            findings.append({"check": "XML-RPC", "status": "pass", "message": "xmlrpc.php not accessible"})
            severity_counts["pass"] += 1

        # 8. WordPress version exposure
        try:
            resp = await client.get(settings.WP_URL, timeout=15, follow_redirects=True)
            version_match = re.search(r'<meta\s+name=["\']generator["\']\s+content=["\']WordPress\s+([\d.]+)', resp.text, re.I)
            if version_match:
                findings.append({
                    "check": "WordPress Version Exposure",
                    "status": "info",
                    "message": f"WordPress version {version_match.group(1)} exposed in meta generator tag",
                })
                severity_counts["info"] += 1
            else:
                findings.append({
                    "check": "WordPress Version Exposure",
                    "status": "pass",
                    "message": "WordPress version not exposed in meta tags",
                })
                severity_counts["pass"] += 1
        except Exception:
            pass

    # Calculate grade
    if severity_counts["critical"] > 0:
        grade = "D"
    elif severity_counts["warning"] > 3:
        grade = "C"
    elif severity_counts["warning"] > 0:
        grade = "B"
    else:
        grade = "A"

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "scanned_at": started.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "grade": grade,
        "severity_counts": severity_counts,
        "total_findings": len(findings),
        "findings": findings,
        "ssl": ssl_info,
    }
