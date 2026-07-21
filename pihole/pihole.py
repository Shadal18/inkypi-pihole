from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session
import logging
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)


class PiHole(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        host = (settings.get("host") or "").strip().rstrip("/")
        api_token = (settings.get("api_token") or "").strip()

        if not host:
            raise RuntimeError("Pi-hole URL is not configured.")

        warning_threshold = float(self._coerce_number(settings.get("warning_threshold"), 10))
        alert_threshold = float(self._coerce_number(settings.get("alert_threshold"), 25))

        snapshot, error = self._collect_snapshot(host, api_token)
        if snapshot is None:
            raise RuntimeError(f"Could not connect to Pi-hole: {error}")

        template_params = {
            "host": host,
            "snapshot": snapshot,
            "status_level": self._derive_status_level(snapshot, warning_threshold, alert_threshold),
            "layout_mode": (settings.get("layout_mode") or "dashboard").strip().lower(),
            "show_status": self._is_true(settings.get("show_status", "true")),
            "show_totals": self._is_true(settings.get("show_totals", "true")),
            "show_clients": self._is_true(settings.get("show_clients", "true")),
            "show_queries": self._is_true(settings.get("show_queries", "true")),
            "show_chart": self._is_true(settings.get("show_chart", "true")),
            "show_destinations": self._is_true(settings.get("show_destinations", "false")),
            "mask_client_names": self._is_true(settings.get("mask_client_names", "false")),
            "mask_domains": self._is_true(settings.get("mask_domains", "false")),
            "accent_panel": settings.get("accent_panel", "#000000"),
            "accent_info": settings.get("accent_info", "#0000ff"),
            "accent_alert": settings.get("accent_alert", "#ff0000"),
            "accent_warn": settings.get("accent_warn", "#ffcc00"),
            "accent_ok": settings.get("accent_ok", "#008000"),
            "plugin_settings": settings,
        }

        logger.warning("Pi-hole snapshot for render: %r", snapshot)
        return self.render_image(dimensions, "pihole.html", "pihole.css", template_params)

    # ------------------------------------------------------------------
    # Session-based auth (Pi-hole v6+)
    # ------------------------------------------------------------------

    def _login(self, session, host, password):
        if not password:
            return None
        try:
            response = session.post(
                f"{host}/api/auth",
                json={"password": password},
                timeout=10,
                verify=False,
            )
            if response.status_code == 200:
                data = response.json() or {}
                sid = (data.get("session") or {}).get("sid")
                if sid:
                    return sid
            logger.warning(
                "Pi-hole login failed (status %s): %s", response.status_code, response.text[:200]
            )
        except Exception as exc:
            logger.error("Pi-hole login request failed: %s", exc)
        return None

    def _logout(self, session, host, sid):
        if not sid:
            return
        try:
            session.delete(
                f"{host}/api/auth",
                headers={"X-FTL-SID": sid},
                timeout=5,
                verify=False,
            )
        except Exception as exc:
            logger.debug("Pi-hole logout failed (non-fatal): %s", exc)

    def _collect_snapshot(self, host, api_token):
        session = get_http_session()
        sid = self._login(session, host, api_token)

        try:
            summary = self._fetch_summary(session, host, sid, api_token)
        except Exception as exc:
            logger.error("Pi-hole summary fetch failed: %s", exc)
            self._logout(session, host, sid)
            return None, str(exc)

        blocking = self._fetch_blocking(session, host, sid, api_token)
        queries = self._fetch_queries(session, host, sid, api_token)
        top_clients = self._fetch_top_clients(session, host, sid, api_token)
        top_domains = self._fetch_top_domains(session, host, sid, api_token)
        upstreams = self._fetch_upstreams(session, host, sid, api_token)
        history = self._fetch_history(session, host, sid, api_token)

        self._logout(session, host, sid)

        queries_block = summary.get("queries") if isinstance(summary.get("queries"), dict) else {}
        clients_block = summary.get("clients") if isinstance(summary.get("clients"), dict) else {}
        gravity_block = summary.get("gravity") if isinstance(summary.get("gravity"), dict) else {}

        total_queries = int(self._coerce_number(
            self._pick(queries_block, ["total"], None)
            if queries_block else self._pick(summary, ["queries", "dns_queries_today", "total_queries"], 0),
            0,
        ))
        blocked_queries = int(self._coerce_number(
            self._pick(queries_block, ["blocked"], None)
            if queries_block else self._pick(summary, ["blocked", "ads_blocked_today", "queries_blocked"], 0),
            0,
        ))
        blocked_percent = round(float(self._coerce_number(
            self._pick(queries_block, ["percent_blocked"], None)
            if queries_block else self._pick(summary, ["percent_blocked", "ads_percentage_today"], 0),
            0,
        )), 1)
        unique_clients = int(self._coerce_number(
            self._pick(clients_block, ["active", "total"], None)
            if clients_block else self._pick(summary, ["clients", "unique_clients"], 0),
            0,
        ))
        domains_on_blocklist = int(self._coerce_number(
            self._pick(gravity_block, ["domains_being_blocked"], None)
            if gravity_block else self._pick(summary, ["domains_being_blocked", "gravity_size"], 0),
            0,
        ))

        snapshot = {
            "blocking_enabled": blocking,
            "total_queries": total_queries,
            "blocked_queries": blocked_queries,
            "blocked_percent": blocked_percent,
            "unique_clients": unique_clients,
            "domains_on_blocklist": domains_on_blocklist,
            "top_clients": self._normalize_top_clients(top_clients, 5),
            "recent_queries": self._normalize_recent_queries(queries, 6),
            "top_domains": self._normalize_top_domains(top_domains, 5),
            "top_upstreams": self._normalize_named_series(upstreams, 4),
            "chart_bars": self._build_chart_bars(history),
        }

        if not snapshot["chart_bars"]:
            snapshot["chart_bars"] = [{"total_pct": 0, "blocked_pct": 0}]

        return snapshot, None

    def _headers(self, sid):
        headers = {"Accept": "application/json"}
        if sid:
            headers["X-FTL-SID"] = sid
        return headers

    def _api_get(self, session, url, sid, params=None):
        response = session.get(
            url,
            headers=self._headers(sid),
            params=params or {},
            timeout=10,
            verify=False,
        )
        response.raise_for_status()
        return response.json()

    def _safe_get(self, session, url, sid, params=None, default=None):
        try:
            return self._api_get(session, url, sid, params=params)
        except Exception as exc:
            logger.warning("Pi-hole request failed for %s: %s", url, exc)
            return default

    def _fetch_summary(self, session, host, sid, api_token):
        data = self._safe_get(session, f"{host}/api/stats/summary", sid, default=None)
        if isinstance(data, dict) and data:
            return data

        data = self._safe_get(
            session,
            f"{host}/admin/api.php",
            sid,
            params={"summaryRaw": "", "auth": api_token} if api_token else {"summaryRaw": ""},
            default=None,
        )
        if isinstance(data, dict) and data:
            return data

        raise RuntimeError("Pi-hole summary endpoint returned no usable data")

    def _fetch_blocking(self, session, host, sid, api_token):
        data = self._safe_get(session, f"{host}/api/dns/blocking", sid, default=None)
        if isinstance(data, dict) and "blocking" in data:
            value = data.get("blocking")
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() == "enabled"

        legacy = self._safe_get(
            session,
            f"{host}/admin/api.php",
            sid,
            params={"summaryRaw": "", "auth": api_token} if api_token else {"summaryRaw": ""},
            default={},
        )
        if isinstance(legacy, dict):
            status = str(legacy.get("status", "")).lower()
            if status == "enabled":
                return True
            if status == "disabled":
                return False

        return None

    def _fetch_queries(self, session, host, sid, api_token):
        data = self._safe_get(session, f"{host}/api/queries", sid, params={"length": 25}, default=None)
        if data:
            return data

        data = self._safe_get(
            session,
            f"{host}/admin/api.php",
            sid,
            params={"getAllQueries": "", "auth": api_token} if api_token else {"getAllQueries": ""},
            default=[],
        )
        return data or []

    def _fetch_top_clients(self, session, host, sid, api_token):
        data = self._safe_get(session, f"{host}/api/stats/top_clients", sid, default=None)
        if data:
            return data

        data = self._safe_get(
            session,
            f"{host}/admin/api.php",
            sid,
            params={"topClients": "10", "auth": api_token} if api_token else {"topClients": "10"},
            default={},
        )
        return data or {}

    def _fetch_top_domains(self, session, host, sid, api_token):
        data = self._safe_get(session, f"{host}/api/stats/top_domains", sid, default=None)
        if data:
            return data

        data = self._safe_get(
            session,
            f"{host}/admin/api.php",
            sid,
            params={"topItems": "10", "auth": api_token} if api_token else {"topItems": "10"},
            default={},
        )
        return data or {}

    def _fetch_upstreams(self, session, host, sid, api_token):
        data = self._safe_get(session, f"{host}/api/stats/upstreams", sid, default=[])
        return data or []

    def _fetch_history(self, session, host, sid, api_token):
        data = self._safe_get(session, f"{host}/api/history", sid, default=None)
        if data:
            return data

        data = self._safe_get(
            session,
            f"{host}/admin/api.php",
            sid,
            params={"overTimeData10mins": "", "auth": api_token} if api_token else {"overTimeData10mins": ""},
            default={},
        )
        return data or {}

    def _pick(self, source, keys, default):
        if not isinstance(source, dict):
            return default
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
        return default

    def _coerce_number(self, value, default=0):
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                cleaned = value.replace(",", "").strip()
                if cleaned == "":
                    return default
                if "." in cleaned:
                    return float(cleaned)
                return int(cleaned)
            except Exception:
                return default
        if isinstance(value, dict):
            for key in ("count", "queries", "value", "total", "num"):
                if key in value:
                    return self._coerce_number(value[key], default)
            return default
        if isinstance(value, list):
            if not value:
                return default
            total = 0
            found = False
            for item in value:
                num = self._coerce_number(item, None)
                if isinstance(num, (int, float)):
                    total += num
                    found = True
            return total if found else default
        return default

    def _normalize_named_series(self, payload, limit):
        rows = []

        if isinstance(payload, dict):
            for key in ("data", "items", "upstreams", "forward_destinations"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break

        if isinstance(payload, dict):
            for name, count in list(payload.items())[:limit]:
                if isinstance(count, (dict, list, str, int, float, bool)):
                    rows.append({"name": str(name), "count": int(self._coerce_number(count, 0))})
            return rows

        if not isinstance(payload, list):
            return rows

        for item in payload[:limit]:
            if isinstance(item, dict):
                name = (
                    item.get("name")
                    or item.get("domain")
                    or item.get("client")
                    or item.get("ip")
                    or item.get("upstream")
                    or item.get("item")
                    or "unknown"
                )
                count = (
                    item.get("count")
                    or item.get("queries")
                    or item.get("value")
                    or item.get("frequency")
                    or item.get("total")
                    or 0
                )
                rows.append({"name": str(name), "count": int(self._coerce_number(count, 0))})
            elif isinstance(item, list) and len(item) >= 2:
                rows.append({"name": str(item[0]), "count": int(self._coerce_number(item[1], 0))})

        return rows

    def _normalize_top_clients(self, payload, limit):
        if isinstance(payload, dict):
            for key in ("clients", "top_clients"):
                entries = payload.get(key)
                if isinstance(entries, dict):
                    return [
                        {"name": str(name), "count": int(self._coerce_number(count, 0))}
                        for name, count in list(entries.items())[:limit]
                    ]
                if isinstance(entries, list):
                    return self._normalize_named_series(entries, limit)
        return self._normalize_named_series(payload, limit)

    def _normalize_top_domains(self, payload, limit):
        if isinstance(payload, dict):
            for key in ("domains", "top_queries", "top_ads"):
                entries = payload.get(key)
                if isinstance(entries, dict):
                    return [
                        {"name": str(name), "count": int(self._coerce_number(count, 0))}
                        for name, count in list(entries.items())[:limit]
                    ]
                if isinstance(entries, list):
                    return self._normalize_named_series(entries, limit)
        return self._normalize_named_series(payload, limit)

    def _normalize_recent_queries(self, payload, limit):
        rows = []

        if isinstance(payload, dict):
            for key in ("queries", "data"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break

        if not isinstance(payload, list):
            return rows

        for item in payload[:limit]:
            if isinstance(item, dict):
                client_field = item.get("client")
                if isinstance(client_field, dict):
                    client_name = client_field.get("name") or client_field.get("ip") or "unknown"
                else:
                    client_name = client_field or item.get("client_name") or item.get("ip") or "unknown"

                rows.append(
                    {
                        "domain": str(item.get("domain") or item.get("name") or item.get("query") or "unknown"),
                        "client": str(client_name),
                        "status": str(item.get("status") or item.get("reply") or item.get("type") or "query"),
                    }
                )
            elif isinstance(item, list):
                rows.append(
                    {
                        "domain": str(item[0] if len(item) > 0 else "unknown"),
                        "client": str(item[1] if len(item) > 1 else "unknown"),
                        "status": str(item[2] if len(item) > 2 else "query"),
                    }
                )

        return rows

    def _build_chart_bars(self, payload):
        values = []
        blocked = []

        if isinstance(payload, dict) and isinstance(payload.get("history"), list):
            for item in payload["history"][-24:]:
                if isinstance(item, dict):
                    values.append(int(self._coerce_number(item.get("total"), 0)))
                    blocked.append(int(self._coerce_number(item.get("blocked"), 0)))

        elif isinstance(payload, dict) and isinstance(payload.get("domains_over_time"), dict):
            domains = payload.get("domains_over_time", {})
            ads = payload.get("ads_over_time", {})
            keys = sorted(domains.keys(), key=lambda x: int(x))[-24:]
            for key in keys:
                values.append(int(self._coerce_number(domains.get(key), 0)))
                blocked.append(int(self._coerce_number(ads.get(key), 0)))

        elif isinstance(payload, list):
            for item in payload[-24:]:
                if isinstance(item, dict):
                    values.append(int(self._coerce_number(
                        item.get("count") or item.get("queries") or item.get("total") or item.get("value") or 0, 0
                    )))
                    blocked.append(int(self._coerce_number(
                        item.get("blocked") or item.get("ads") or item.get("ads_blocked") or 0, 0
                    )))
                elif isinstance(item, list):
                    values.append(int(self._coerce_number(item[0] if len(item) > 0 else 0, 0)))
                    blocked.append(int(self._coerce_number(item[1] if len(item) > 1 else 0, 0)))

        if not values:
            return [{"total_pct": 0, "blocked_pct": 0}]

        peak = max(max(values), 1)
        bars = []
        for total_value, blocked_value in zip(values, blocked):
            total_pct = round((total_value / peak) * 100)
            blocked_pct = round((min(blocked_value, total_value) / total_value) * 100) if total_value > 0 else 0
            bars.append({"total_pct": total_pct, "blocked_pct": blocked_pct})
        return bars

    def _derive_status_level(self, snapshot, warning_threshold=10, alert_threshold=25):
        if snapshot.get("blocking_enabled") is False:
            return "disabled"
        percent = self._coerce_number(snapshot.get("blocked_percent", 0), 0)
        if percent >= alert_threshold:
            return "shielding"
        if percent >= warning_threshold:
            return "active"
        return "light"

    def _is_true(self, value):
        return str(value).strip().lower() == "true"