from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

import requests
from backend.leagues import CURRENT_SEASON, LEAGUE_IDS
from backend.prediction import estimate_team_strengths, predict_fixture


@dataclass
class ProviderError(Exception):
    message: str
    status_code: int = 503
    upstream_status: Optional[int] = None

    def __str__(self) -> str:
        return self.message


class FootballProvider(Protocol):
    def get_fixtures(self, league_code: str, days: int) -> List[Dict[str, Any]]: ...
    def get_standings(self, league_code: str) -> List[Dict[str, Any]]: ...
    def get_predictions(self, league_code: str, days: int = 14) -> List[Dict[str, Any]]: ...


class _TTLCache:
    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = int(ttl_seconds)
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        row = self._store.get(key)
        if not row:
            return None
        expires_at, value = row
        if time.time() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def get_stale(self, key: str) -> Any:
        row = self._store.get(key)
        if not row:
            return None
        _expires_at, value = row
        return value

    def set(self, key: str, value: Any) -> Any:
        self._store[key] = (time.time() + self.ttl_seconds, value)
        return value


class _ThreadSafeTTLCache:
    def __init__(self):
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        now = time.time()
        with self._lock:
            row = self._store.get(key)
            if not row:
                return None
            expires_at, value = row
            if now >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    def get_stale(self, key: str) -> Any:
        with self._lock:
            row = self._store.get(key)
            if not row:
                return None
            _expires_at, value = row
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> Any:
        with self._lock:
            self._store[key] = (time.time() + int(ttl_seconds), value)
        return value


class APIFootballProvider:
    """
    Migration stub. Keeps route handlers untouched.
    """

    CODE_TO_KEY: Dict[str, str] = {
        "PL": "epl",
        "PD": "laliga",
        "SA": "seriea",
        "FL1": "ligue1",
    }
    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(self):
        self.base_url = self.BASE_URL
        self.timeout_seconds = 10
        self.api_key = os.getenv("APIFOOTBALL_API_KEY")
        if not self.api_key:
            raise RuntimeError("APIFOOTBALL_API_KEY not set in environment variables")
        self.default_season = int(CURRENT_SEASON)
        self.standings_ttl_seconds = 1800
        self.fixtures_ttl_seconds = 600
        self.predictions_ttl_seconds = 600
        self.cache = _ThreadSafeTTLCache()

    def _league_meta(self, league_code: str) -> Dict[str, Any]:
        league_key = self.CODE_TO_KEY.get(league_code, "")
        league_id = LEAGUE_IDS.get(league_key) if league_key else None
        return {"league_id": league_id, "season": self.default_season}

    def _headers(self) -> Dict[str, str]:
        return {
            "x-apisports-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "FootballAnalyticsHub/1.0",
        }

    def _request(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status_code, payload = self._request_with_status(path, params)
        if status_code != 200:
            return None
        return payload

    def _request_with_status(self, path: str, params: Dict[str, Any]) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        headers = self._headers()
        if not headers:
            return None, None
        url = f"{self.base_url}{path}"
        print("Calling API-Football:", url)
        print("Params:", params)
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout_seconds,
            )
            payload = response.json() if response.content else {}
            return int(response.status_code), payload
        except Exception:
            return None, None

    def _parse_matchday(self, round_text: Any) -> int:
        try:
            text = str(round_text or "")
            tail = text.split("-")[-1].strip()
            return int(tail) if tail.isdigit() else 0
        except Exception:
            return 0

    def _cache_key(self, kind: str, league_code: str, season: int, params: Dict[str, Any]) -> str:
        parts = [f"{k}={params[k]}" for k in sorted(params.keys())]
        return f"{kind}:{league_code}:{season}:{'&'.join(parts)}"

    def _to_prob(self, value: Any) -> float:
        try:
            s = str(value or "").strip().replace("%", "")
            if s == "":
                return 0.0
            return float(s) / 100.0
        except Exception:
            return 0.0

    def get_fixtures(self, league_code: str, days: int) -> List[Dict[str, Any]]:
        meta = self._league_meta(league_code)
        league_id = meta.get("league_id")
        if league_id is None:
            return []

        days = max(1, min(int(days), 60))
        today = datetime.now(timezone.utc).date()
        end = today + timedelta(days=days)
        season = int(meta.get("season") or self.default_season)
        query_params = {
            "league": league_id,
            "season": season,
            "from": today.isoformat(),
            "to": end.isoformat(),
        }
        cache_key = self._cache_key("fixtures", league_code, season, query_params)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self._request(
            "/fixtures",
            query_params,
        )
        if payload is None:
            stale = self.cache.get_stale(cache_key)
            return stale if stale is not None else []
        fixtures_raw = payload.get("response", []) or []
        out: List[Dict[str, Any]] = []
        for item in fixtures_raw:
            fixture = item.get("fixture", {}) or {}
            teams = item.get("teams", {}) or {}
            home_team = teams.get("home", {}) or {}
            away_team = teams.get("away", {}) or {}
            out.append(
                {
                    "utcDate": fixture.get("date"),
                    "matchday": self._parse_matchday((item.get("league", {}) or {}).get("round")),
                    "competition": league_code,
                    "venue": str((fixture.get("venue") or {}).get("name") or "Home"),
                    "home": str(home_team.get("name") or ""),
                    "away": str(away_team.get("name") or ""),
                    "match_id": int(fixture.get("id") or 0),
                    "utc_date": fixture.get("date"),
                    "status": str(((fixture.get("status") or {}).get("short")) or ""),
                    "home_team_id": int(home_team.get("id") or 0),
                    "home_team_name": str(home_team.get("name") or ""),
                    "away_team_id": int(away_team.get("id") or 0),
                    "away_team_name": str(away_team.get("name") or ""),
                }
            )
        out.sort(key=lambda x: str(x.get("utcDate") or ""))
        return self.cache.set(cache_key, out, self.fixtures_ttl_seconds)

    def get_standings(self, league_code: str) -> List[Dict[str, Any]]:
        meta = self._league_meta(league_code)
        league_id = meta.get("league_id")
        if league_id is None:
            return []
        season = int(meta.get("season") or self.default_season)
        query_params = {"league": league_id, "season": season}
        cache_key = self._cache_key("standings", league_code, season, query_params)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self._request(
            "/standings",
            query_params,
        )
        if payload is None:
            stale = self.cache.get_stale(cache_key)
            return stale if stale is not None else []
        response_rows = payload.get("response", []) or []
        if not response_rows:
            return self.cache.set(cache_key, [], self.standings_ttl_seconds)
        league_info = response_rows[0].get("league", {}) or {}
        standings_groups = league_info.get("standings", []) or []
        if not standings_groups:
            return self.cache.set(cache_key, [], self.standings_ttl_seconds)
        table_rows = standings_groups[0] or []
        out: List[Dict[str, Any]] = []
        for row in table_rows:
            team = row.get("team", {}) or {}
            goals = row.get("all", {}) or {}
            gf = int((goals.get("goals") or {}).get("for") or 0)
            ga = int((goals.get("goals") or {}).get("against") or 0)
            out.append(
                {
                    "position": int(row.get("rank") or 0),
                    "teamName": str(team.get("name") or ""),
                    "teamShort": str(team.get("code") or team.get("name") or ""),
                    "playedGames": int((goals.get("played") or 0)),
                    "won": int((goals.get("win") or 0)),
                    "draw": int((goals.get("draw") or 0)),
                    "lost": int((goals.get("lose") or 0)),
                    "points": int(row.get("points") or 0),
                    "goalsFor": gf,
                    "goalsAgainst": ga,
                    "goalDifference": int(row.get("goalsDiff") or (gf - ga)),
                    "team": str(team.get("name") or ""),
                }
            )
        sorted_out = sorted(out, key=lambda x: x["position"])
        return self.cache.set(cache_key, sorted_out, self.standings_ttl_seconds)

    def get_predictions(self, league_code: str, days: int = 14) -> List[Dict[str, Any]]:
        meta = self._league_meta(league_code)
        season = int(meta.get("season") or self.default_season)
        cache_key = self._cache_key(
            "predictions",
            league_code,
            season,
            {"days": int(days)},
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            fixtures = self.get_fixtures(league_code, int(days))
            if not fixtures:
                stale = self.cache.get_stale(cache_key)
                return stale if stale is not None else []
            out: List[Dict[str, Any]] = []
            for fx in fixtures:
                match_id = int(fx.get("match_id") or 0)
                if match_id <= 0:
                    continue
                status_code, payload = self._request_with_status("/predictions", {"fixture": match_id})
                if status_code is not None and 400 <= status_code < 500:
                    stale = self.cache.get_stale(cache_key)
                    return stale if stale is not None else []
                if status_code != 200 or payload is None:
                    stale = self.cache.get_stale(cache_key)
                    return stale if stale is not None else []
                items = payload.get("response", []) or []
                if not items:
                    continue
                api_pred = items[0].get("predictions", {}) or {}
                percent = api_pred.get("percent", {}) or {}
                pred = {
                    "home_win": self._to_prob(percent.get("home")),
                    "draw": self._to_prob(percent.get("draw")),
                    "away_win": self._to_prob(percent.get("away")),
                    "lambda_h": 0.0,
                    "lambda_a": 0.0,
                    "xgH": 0.0,
                    "xgA": 0.0,
                }
                out.append(
                    {
                        "match_id": match_id,
                        "utc_date": fx.get("utc_date"),
                        "status": fx.get("status"),
                        "home_team_id": int(fx.get("home_team_id") or 0),
                        "home_team_name": fx.get("home_team_name"),
                        "away_team_id": int(fx.get("away_team_id") or 0),
                        "away_team_name": fx.get("away_team_name"),
                        "prediction": pred,
                    }
                )
            return self.cache.set(cache_key, out, self.predictions_ttl_seconds)
        except Exception:
            stale = self.cache.get_stale(cache_key)
            return stale if stale is not None else []


def get_provider() -> FootballProvider:
    from .apifootball_provider import ApiFootballProvider

    print("[get_provider] selected: ApiFootballProvider")
    return ApiFootballProvider()
