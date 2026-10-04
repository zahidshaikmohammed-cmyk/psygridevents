"""Live market confirmation: how has the stock reacted to the event, and is the move still tradeable?

Computed only from real PSYGRID 1-minute candles held in a MarketSnapshot.
Every metric that cannot be computed from available data is None with an
explicit reason in `uncertainty`; nothing is filled in.

Three clocks:
  EVENT CLOCK        -- when the information became public (`public_at`).
  MARKET CLOCK       -- when the stock started reacting (`reaction_start_at`,
                        `event_to_price_latency_minutes`).
  OPPORTUNITY CLOCK  -- whether meaningful movement remains
                        (`remaining_opportunity`, `exhaustion_level`).
"""
from __future__ import annotations

import copy
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .market_confirmation import MarketConfirmationAssessment
from .market_response import MarketResponseAssessment, MarketResponseEngine
from .market_snapshot import MarketSnapshot

REFERENCE_BAR_RANGE = 0.0015  # 0.15% typical 1m range for a liquid large cap; used only to scale bands


@dataclass(frozen=True)
class ReactionMetrics:
    symbol: str
    as_of: datetime
    data_status: str  # LIVE | STALE | NO_DATA | TIME_ERROR | NO_SESSION_DATA
    market_health: str
    latest_bar_at: datetime | None
    market_data_age_seconds: float | None
    session_relation: str
    expected_direction: str
    reaction_direction: str  # up | down | flat | unknown
    direction_source: str  # documented | market_reaction | none
    last_price: float | None
    previous_close: float | None
    today_open: float | None
    day_change: float | None
    gap: float | None
    intraday_return: float | None
    baseline_kind: str | None
    baseline_price: float | None
    baseline_at: datetime | None
    move_since_event: float | None
    aligned_move: float | None
    peak_aligned_move: float | None
    peak_at: datetime | None
    retracement_from_peak: float | None
    reaction_start_at: datetime | None
    event_to_price_latency_minutes: float | None
    minutes_since_reaction_start: float | None
    speed_to_half_peak_minutes: float | None
    persistence: float | None
    bars_since_event: int
    volume_ratio: float | None
    relative_volume_historical: float | None
    zero_volume_recent: bool
    vwap: float | None
    vwap_state: str  # above | below | at | unavailable
    vwap_aligned: bool | None
    distance_from_vwap: float | None
    momentum_5m: float | None
    momentum_15m: float | None
    relative_strength: float | None
    relative_strength_basis: str | None
    sector: str | None
    sector_relative_strength: float | None
    candle_body_ratio: float | None
    candle_close_location: float | None
    pullback_depth: float | None
    retest_held: bool | None
    distance_from_initial_reaction: float | None
    volatility_unit: float | None
    extension_units: float | None
    exhaustion_score: float
    exhaustion_level: str  # LOW | MEDIUM | HIGH | EXHAUSTED | UNKNOWN
    exhaustion_flags: tuple[str, ...]
    remaining_opportunity: float
    response_state: str  # no_response | early | developing | late | exhausted | unknown
    confirmation_status: str  # confirmed | contradicted | mixed | untested
    confirmation_reason: str
    uncertainty: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat()
            elif isinstance(value, float):
                data[key] = round(value, 6) if math.isfinite(value) else None
        return data

    # Bridges to the existing CP10/CP5 contracts so CP9 exhaustion and the CP11
    # signal engine keep making the decisions they were designed to make.
    def to_market_response(self, event_id: str, expected_direction: str) -> MarketResponseAssessment | None:
        if self.baseline_price is None:
            return None
        alignment = "unknown"
        if self.aligned_move is not None:
            threshold = 0.002 * self._scale()
            alignment = "aligned" if self.aligned_move > threshold else "opposed" if self.aligned_move < -threshold else "flat"
        velocity = "unavailable"
        if self.momentum_5m is not None and self.aligned_move is not None and self.minutes_since_reaction_start:
            sign = 1.0 if (self.aligned_move or 0) >= 0 else -1.0
            aligned_momentum = self.momentum_5m * (1 if self.reaction_direction == "up" else -1) * sign
            average_rate = abs(self.aligned_move) / max(1.0, self.minutes_since_reaction_start) * 5
            velocity = "accelerating" if aligned_momentum > average_rate * 1.1 else (
                "decelerating" if aligned_momentum < average_rate * 0.9 else "steady")
        return MarketResponseAssessment(
            event_id=event_id, asset=self.symbol, as_of=self.as_of, expected_direction=expected_direction,
            alignment=alignment, price_displacement=self.move_since_event,
            peak_aligned_displacement=self.peak_aligned_move, relative_performance=self.relative_strength,
            sector_relative_performance=self.sector_relative_strength,
            volume_ratio=self.volume_ratio if self.volume_ratio is not None else self.relative_volume_historical,
            volume_state=(
                "unavailable" if (self.volume_ratio is None and self.relative_volume_historical is None)
                else "elevated" if max(self.volume_ratio or 0, self.relative_volume_historical or 0) >= 1.2 else "normal"
            ),
            vwap_state="unavailable" if self.vwap_aligned is None else ("aligned" if self.vwap_aligned else "opposed"),
            velocity_state=velocity, reversal=bool(self.retracement_from_peak and self.retracement_from_peak >= 0.4),
            response_state=self.response_state, observations_used=self.bars_since_event + 1,
            baseline_timestamp=self.baseline_at, latest_timestamp=self.latest_bar_at, uncertainty=self.uncertainty,
            reason=self.confirmation_reason,
        )

    def to_confirmation(self, event_id: str, expected_direction: str) -> MarketConfirmationAssessment:
        return MarketConfirmationAssessment(
            event_id=event_id, status=self.confirmation_status, expected_direction=expected_direction,
            observed_return=self.move_since_event, relative_return=self.relative_strength,
            volume_ratio=self.volume_ratio, vwap_relation=(
                "not_available" if self.vwap_aligned is None else "aligned" if self.vwap_aligned else "opposed"),
            sector_alignment="not_available" if self.sector_relative_strength is None else (
                "aligned" if (self.sector_relative_strength or 0) * self._sign() > 0 else "opposed"),
            observations_used=self.bars_since_event + 1, window_start=self.baseline_at, window_end=self.latest_bar_at,
            score=0.0, reason=self.confirmation_reason,
        )

    def _sign(self) -> float:
        return 1.0 if self.reaction_direction == "up" else -1.0 if self.reaction_direction == "down" else 0.0

    def _scale(self) -> float:
        if not self.volatility_unit:
            return 1.0
        return max(0.75, min(3.0, self.volatility_unit / REFERENCE_BAR_RANGE))


@dataclass(frozen=True)
class ReactionConfig:
    min_move_floor: float = 0.002
    move_threshold_units: float = 2.5
    confirm_min_bars: int = 3
    confirm_min_persistence: float = 0.6
    confirm_volume_ratio: float = 1.5
    rs_threshold: float = 0.002
    extension_exhausted_units: float = 30.0
    extension_high_units: float = 18.0
    far_from_vwap_units: float = 8.0
    reversal_retracement: float = 0.4
    late_session_minutes_to_close: float = 45.0


def _median(values: list[float]) -> float | None:
    cleaned = [value for value in values if value is not None and math.isfinite(value)]
    return statistics.median(cleaned) if cleaned else None


class ReactionAnalyzer:
    def __init__(self, response_engine: MarketResponseEngine, config: ReactionConfig | None = None) -> None:
        self.response_engine = response_engine
        self.config = config or ReactionConfig()

    def analyze(
        self,
        snapshot: MarketSnapshot,
        symbol: str,
        *,
        public_at: datetime | None,
        session_relation: str,
        expected_direction: str,
        market_open_epoch: float,
        market_close_epoch: float,
        historical_profiles: list[dict[str, float]] | None = None,
    ) -> ReactionMetrics:
        cfg = self.config
        as_of = snapshot.as_of
        uncertainty: list[str] = []
        series = snapshot.series.get(symbol)
        freshness = snapshot.symbol_freshness(symbol)
        health = snapshot.health()

        def empty(status: str, reason: str) -> ReactionMetrics:
            return ReactionMetrics(
                symbol=symbol, as_of=as_of, data_status=status, market_health=health, latest_bar_at=None,
                market_data_age_seconds=snapshot.data_age_seconds(symbol) if series else None,
                session_relation=session_relation, expected_direction=expected_direction, reaction_direction="unknown",
                direction_source="none", last_price=None, previous_close=series.previous_close if series else None,
                today_open=series.today_open if series else None, day_change=None, gap=None, intraday_return=None,
                baseline_kind=None, baseline_price=None, baseline_at=None, move_since_event=None, aligned_move=None,
                peak_aligned_move=None, peak_at=None, retracement_from_peak=None, reaction_start_at=None,
                event_to_price_latency_minutes=None, minutes_since_reaction_start=None, speed_to_half_peak_minutes=None,
                persistence=None, bars_since_event=0, volume_ratio=None, relative_volume_historical=None,
                zero_volume_recent=False, vwap=None, vwap_state="unavailable", vwap_aligned=None, distance_from_vwap=None,
                momentum_5m=None, momentum_15m=None, relative_strength=None, relative_strength_basis=None,
                sector=snapshot.sector_of.get(symbol), sector_relative_strength=None, candle_body_ratio=None,
                candle_close_location=None, pullback_depth=None, retest_held=None, distance_from_initial_reaction=None,
                volatility_unit=None, extension_units=None, exhaustion_score=0.0, exhaustion_level="UNKNOWN",
                exhaustion_flags=(), remaining_opportunity=0.0, response_state="unknown",
                confirmation_status="untested", confirmation_reason=reason, uncertainty=(reason,),
            )

        if series is None or len(series) == 0:
            return empty("NO_DATA", "PSYGRID has no candles for this symbol in the current snapshot.")
        if freshness == "TIME_ERROR":
            return empty("TIME_ERROR", "Latest candle is ahead of the engine clock; market data rejected.")

        n = len(series)
        closes = series.close
        last = closes[-1]
        latest_at = datetime.fromtimestamp(series.ts[-1], tz=timezone.utc)
        previous_close = series.previous_close
        today_open = series.today_open or series.open[0]
        day_change = (last / previous_close - 1.0) if previous_close else None
        gap = (today_open / previous_close - 1.0) if previous_close else None
        intraday = last / today_open - 1.0 if today_open else None
        if previous_close is None:
            uncertainty.append("PSYGRID did not provide previous_close; gap/day change unavailable.")

        # ------------------------------------------------------- baseline (event clock)
        public_epoch = public_at.timestamp() if public_at else None
        baseline_kind: str | None
        baseline_price: float | None
        baseline_epoch: float | None
        first_reaction_index: int
        if session_relation == "DURING_SESSION" and public_epoch is not None:
            index = series.index_at_or_before(public_epoch - 60.0)  # last bar completed before the event
            if index >= 0:
                baseline_kind, baseline_price, baseline_epoch = "pre_event_bar", closes[index], series.ts[index]
                first_reaction_index = index + 1
            else:
                baseline_kind, baseline_price, baseline_epoch = "session_open", today_open, series.ts[0]
                first_reaction_index = 0
        elif session_relation in {"BEFORE_OPEN", "PRIOR_SESSION"}:
            if previous_close:
                baseline_kind, baseline_price, baseline_epoch = "previous_close", previous_close, None
            else:
                baseline_kind, baseline_price, baseline_epoch = "session_open", today_open, series.ts[0]
                uncertainty.append("Pre-open event without previous_close: baseline is today's open (gap not captured).")
            first_reaction_index = 0
        elif session_relation == "AFTER_CLOSE":
            return empty(freshness, "Event became public after today's close; no live reaction is possible until the next session.")
        else:
            return empty(freshness, "Event public time unknown; reaction cannot be anchored.")

        reaction_closes = list(closes[first_reaction_index:])
        reaction_ts = list(series.ts[first_reaction_index:])
        bars_since = len(reaction_closes)
        pre_ranges = [
            (series.high[i] - series.low[i]) / series.close[i]
            for i in range(0, max(first_reaction_index, min(n, 30)))
            if series.close[i] > 0
        ]
        volatility_unit = _median(pre_ranges) or None
        if volatility_unit is not None:
            volatility_unit = max(volatility_unit, 0.0003)
        threshold = max(cfg.min_move_floor, cfg.move_threshold_units * (volatility_unit or REFERENCE_BAR_RANGE))

        # ------------------------------------------------------- volume
        pre_volumes = list(series.volume[max(0, first_reaction_index - 30):first_reaction_index])
        median_pre = _median(pre_volumes) if len(pre_volumes) >= 5 else None
        zero_recent = n >= 5 and sum(series.volume[-5:]) == 0
        if zero_recent:
            uncertainty.append("Zero traded volume in the last 5 bars (halt, circuit or illiquidity).")
        rvol_hist = None
        if historical_profiles:
            minute = int((series.ts[-1] + 60 - market_open_epoch) // 60)
            bucket = str(max(15, (minute // 15 + 1) * 15))
            history = [profile.get(bucket) for profile in historical_profiles if profile.get(bucket)]
            if len(history) >= 3:
                cumulative = float(sum(series.volume))
                reference = statistics.median(history)
                rvol_hist = round(cumulative / reference, 3) if reference > 0 else None
        else:
            uncertainty.append("No stored historical volume profile yet; historical relative volume unavailable.")

        # ------------------------------------------------------- VWAP / momentum
        vwap_values = series.vwap_series()
        vwap = vwap_values[-1] if vwap_values and math.isfinite(vwap_values[-1]) else None
        momentum_5m = (last / closes[-6] - 1.0) if n >= 6 else None
        momentum_15m = (last / closes[-16] - 1.0) if n >= 16 else None

        # ------------------------------------------------------- relative strength
        relative_strength = None
        rs_basis = None
        sector = snapshot.sector_of.get(symbol)
        sector_rs = None
        if baseline_price and baseline_price > 0:
            move = last / baseline_price - 1.0
            if baseline_kind == "previous_close":
                universe_day = _universe_day_change(snapshot)
                if universe_day is not None:
                    relative_strength, rs_basis = move - universe_day, "universe_median_day_change"
                if sector:
                    sector_day = _sector_day_change(snapshot, sector, exclude=symbol)
                    if sector_day is not None:
                        sector_rs = move - sector_day
            elif baseline_epoch is not None:
                bench, basis = snapshot.benchmark_return(baseline_epoch, series.ts[-1])
                if bench is not None:
                    relative_strength, rs_basis = move - bench, basis
                if sector:
                    sector_return = snapshot.sector_return(sector, baseline_epoch, series.ts[-1], exclude=symbol)
                    if sector_return is not None:
                        sector_rs = move - sector_return
        if relative_strength is None:
            uncertainty.append("Market-relative strength unavailable (benchmark/universe data insufficient).")
        if sector is None:
            uncertainty.append("No configured sector for this symbol; sector-relative strength unavailable.")

        if not baseline_price or bars_since == 0:
            metrics_reason = "Event baseline established; no post-event bar yet."
            result = empty(freshness, metrics_reason)
            return _replace(result, last_price=last, day_change=day_change, gap=gap, intraday_return=intraday,
                            baseline_kind=baseline_kind, baseline_price=baseline_price,
                            baseline_at=_dt(baseline_epoch), latest_bar_at=latest_at, response_state="no_response",
                            market_data_age_seconds=snapshot.data_age_seconds(symbol), vwap=vwap)

        moves = [close / baseline_price - 1.0 for close in reaction_closes]
        last_move = moves[-1]
        if expected_direction in ("positive", "negative"):
            sign = 1.0 if expected_direction == "positive" else -1.0
            direction_source = "documented"
        else:
            sign = 1.0 if last_move > threshold else -1.0 if last_move < -threshold else 0.0
            direction_source = "market_reaction" if sign else "none"
        reaction_direction = "up" if last_move > threshold else "down" if last_move < -threshold else "flat"
        if sign == 0.0:
            aligned = [abs(value) for value in moves]
            aligned_last = None
        else:
            aligned = [value * sign for value in moves]
            aligned_last = aligned[-1]

        peak_index = max(range(len(aligned)), key=lambda i: aligned[i])
        peak = aligned[peak_index]
        retracement = None
        if peak > 0 and aligned_last is not None:
            retracement = max(0.0, (peak - aligned_last) / peak)
        start_index = next((i for i, value in enumerate(aligned) if value >= threshold), None)
        reaction_start_at = _dt(reaction_ts[start_index]) if start_index is not None else None
        reference_epoch = public_epoch if baseline_kind == "pre_event_bar" else market_open_epoch
        latency = None
        if start_index is not None and reference_epoch is not None:
            latency = max(0.0, (reaction_ts[start_index] + 60 - reference_epoch) / 60.0)
        minutes_since_start = None
        if start_index is not None:
            minutes_since_start = (series.ts[-1] - reaction_ts[start_index]) / 60.0
        speed = None
        if start_index is not None and peak > 0:
            half = next((i for i in range(start_index, len(aligned)) if aligned[i] >= 0.5 * peak), None)
            if half is not None:
                speed = (reaction_ts[half] - reaction_ts[start_index]) / 60.0 + 1.0
        persistence = None
        if start_index is not None:
            window = aligned[start_index:]
            persistence = sum(1 for value in window if value >= 0.5 * threshold) / len(window)

        post_volumes = list(series.volume[first_reaction_index + (start_index or 0):][:15])
        volume_ratio = None
        if median_pre and median_pre > 0 and post_volumes:
            volume_ratio = round(statistics.mean(post_volumes) / median_pre, 3)
        elif baseline_kind != "previous_close":
            uncertainty.append("Too few pre-event bars for an intraday volume baseline.")

        vwap_state = "unavailable"
        vwap_aligned = None
        distance_vwap = None
        if vwap:
            distance_vwap = last / vwap - 1.0
            vwap_state = "above" if distance_vwap > 0.0005 else "below" if distance_vwap < -0.0005 else "at"
            if sign:
                vwap_aligned = distance_vwap * sign >= -0.0005

        last3 = range(max(0, n - 3), n)
        bodies = []
        locations = []
        for i in last3:
            span = series.high[i] - series.low[i]
            if span > 0:
                bodies.append(abs(series.close[i] - series.open[i]) / span)
                location = (series.close[i] - series.low[i]) / span
                locations.append(location if sign >= 0 else 1.0 - location)
        candle_body = round(statistics.mean(bodies), 3) if bodies else None
        candle_location = round(statistics.mean(locations), 3) if locations else None

        pullback = None
        retest_held = None
        if peak > 0 and peak_index < len(aligned) - 1:
            after = aligned[peak_index + 1:]
            pullback = max(0.0, (peak - min(after)) / peak)
            if vwap_values and sign:
                touched = False
                for offset in range(peak_index + 1, len(aligned)):
                    i = first_reaction_index + offset
                    level = vwap_values[i]
                    if not math.isfinite(level):
                        continue
                    if (sign > 0 and series.low[i] <= level) or (sign < 0 and series.high[i] >= level):
                        touched = True
                if touched:
                    retest_held = bool(vwap_aligned)

        initial = None
        if start_index is not None and sign:
            initial = (last / reaction_closes[start_index] - 1.0) * sign

        extension_units = None
        if volatility_unit and aligned_last is not None:
            extension_units = round(aligned_last / volatility_unit, 2)

        # ------------------------------------------------------- exhaustion / opportunity clock
        flags: list[str] = []
        score = 0.0
        if extension_units is not None:
            if extension_units >= cfg.extension_exhausted_units:
                flags.append(f"extended_{extension_units:.0f}x_typical_bar_range")
                score += 0.55
            elif extension_units >= cfg.extension_high_units:
                flags.append(f"stretched_{extension_units:.0f}x_typical_bar_range")
                score += 0.3
        if retracement is not None and retracement >= cfg.reversal_retracement:
            flags.append(f"retraced_{retracement:.0%}_from_peak")
            score += 0.45
        if distance_vwap is not None and volatility_unit and sign and distance_vwap * sign / volatility_unit >= cfg.far_from_vwap_units:
            flags.append("far_from_vwap")
            score += 0.15
        if momentum_5m is not None and sign and momentum_5m * sign < -0.5 * threshold and peak > threshold:
            flags.append("momentum_fading")
            score += 0.15
        if post_volumes and len(series.volume) >= 10:
            peak_volume = max(series.volume[first_reaction_index:]) if bars_since else 0
            recent_volume = statistics.mean(series.volume[-5:])
            if peak_volume > 0 and recent_volume < 0.3 * peak_volume and minutes_since_start and minutes_since_start > 10:
                flags.append("volume_climax_faded")
                score += 0.15
        minutes_to_close = (market_close_epoch - as_of.timestamp()) / 60.0
        if 0 <= minutes_to_close <= cfg.late_session_minutes_to_close:
            flags.append("late_session")
            score += 0.15
        if sign == 0.0:
            flags.append("no_directional_reaction")
        score = min(1.0, score)
        level = "LOW" if score < 0.3 else "MEDIUM" if score < 0.55 else "HIGH" if score < 0.8 else "EXHAUSTED"
        remaining = max(0.0, min(1.0, 1.0 - score))

        # ------------------------------------------------------- CP10 staging (volatility scaled)
        scale = max(0.75, min(3.0, (volatility_unit or REFERENCE_BAR_RANGE) / REFERENCE_BAR_RANGE))
        staged = copy.copy(self.response_engine)
        staged.min_move = self.response_engine.min_move * scale
        staged.early_threshold = self.response_engine.early_threshold * scale
        staged.developing_threshold = self.response_engine.developing_threshold * scale
        staged.late_threshold = self.response_engine.late_threshold * scale
        if aligned_last is None:
            response_state = "no_response"
        else:
            alignment = "aligned" if aligned_last > staged.min_move else "opposed" if aligned_last < -staged.min_move else "flat"
            reversal = bool(retracement is not None and retracement >= staged.reversal_retracement and peak_index < len(aligned) - 1)
            response_state, _ = staged._stage(alignment, aligned_last, reversal)
            if level == "EXHAUSTED":
                response_state = "exhausted"

        # ------------------------------------------------------- confirmation (CP5-compatible)
        volume_ok = (volume_ratio is not None and volume_ratio >= cfg.confirm_volume_ratio) or (
            rvol_hist is not None and rvol_hist >= cfg.confirm_volume_ratio)
        rs_ok = relative_strength is not None and sign != 0 and relative_strength * sign >= cfg.rs_threshold
        corroborators = int(volume_ok) + int(bool(vwap_aligned)) + int(rs_ok)
        bars_after_start = (len(aligned) - start_index) if start_index is not None else 0
        if freshness not in ("LIVE", "STALE"):
            confirmation, reason = "untested", f"Market data status is {freshness}."
        elif bars_since < cfg.confirm_min_bars:
            confirmation, reason = "untested", f"Only {bars_since} post-event bar(s); need {cfg.confirm_min_bars}."
        elif expected_direction in ("positive", "negative") and aligned_last is not None and aligned_last <= -threshold and (
            volume_ratio is None or volume_ratio >= 1.0
        ):
            confirmation, reason = "contradicted", (
                f"Price moved {last_move:+.2%} against the documented {expected_direction} direction since the event.")
        elif (
            aligned_last is not None and aligned_last >= threshold and persistence is not None
            and persistence >= cfg.confirm_min_persistence and bars_after_start >= cfg.confirm_min_bars and corroborators >= 2
        ):
            confirmation, reason = "confirmed", (
                f"Move {last_move:+.2%} since event with persistence {persistence:.0%}; corroborated by "
                + ", ".join(
                    part for part, ok in (("volume", volume_ok), ("VWAP", bool(vwap_aligned)), ("relative strength", rs_ok)) if ok
                ) + "."
            )
        else:
            confirmation, reason = "mixed", "Market evidence is developing or only partially corroborated."
        if freshness == "STALE":
            uncertainty.append("Market data is STALE; reaction is not live.")

        return ReactionMetrics(
            symbol=symbol, as_of=as_of, data_status=freshness, market_health=health, latest_bar_at=latest_at,
            market_data_age_seconds=snapshot.data_age_seconds(symbol), session_relation=session_relation,
            expected_direction=expected_direction, reaction_direction=reaction_direction,
            direction_source=direction_source, last_price=last, previous_close=previous_close, today_open=today_open,
            day_change=day_change, gap=gap, intraday_return=intraday, baseline_kind=baseline_kind,
            baseline_price=baseline_price, baseline_at=_dt(baseline_epoch), move_since_event=last_move,
            aligned_move=aligned_last, peak_aligned_move=peak if sign else None,
            peak_at=_dt(reaction_ts[peak_index]) if sign else None, retracement_from_peak=retracement,
            reaction_start_at=reaction_start_at, event_to_price_latency_minutes=latency,
            minutes_since_reaction_start=minutes_since_start, speed_to_half_peak_minutes=speed, persistence=persistence,
            bars_since_event=bars_since, volume_ratio=volume_ratio, relative_volume_historical=rvol_hist,
            zero_volume_recent=zero_recent, vwap=vwap, vwap_state=vwap_state, vwap_aligned=vwap_aligned,
            distance_from_vwap=distance_vwap, momentum_5m=momentum_5m, momentum_15m=momentum_15m,
            relative_strength=relative_strength, relative_strength_basis=rs_basis, sector=sector,
            sector_relative_strength=sector_rs, candle_body_ratio=candle_body, candle_close_location=candle_location,
            pullback_depth=pullback, retest_held=retest_held, distance_from_initial_reaction=initial,
            volatility_unit=volatility_unit, extension_units=extension_units, exhaustion_score=round(score, 3),
            exhaustion_level=level, exhaustion_flags=tuple(flags), remaining_opportunity=round(remaining, 3),
            response_state=response_state, confirmation_status=confirmation, confirmation_reason=reason,
            uncertainty=tuple(dict.fromkeys(uncertainty)),
        )


def _dt(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch is not None else None


def _replace(metrics: ReactionMetrics, **changes: Any) -> ReactionMetrics:
    from dataclasses import replace

    return replace(metrics, **changes)


def _universe_day_change(snapshot: MarketSnapshot) -> float | None:
    key = ("universe_day",)
    if key in snapshot._median_cache:
        return snapshot._median_cache[key]
    values = [
        series.close[-1] / series.previous_close - 1.0
        for series in snapshot.series.values()
        if len(series) and series.previous_close
    ]
    result = statistics.median(values) if len(values) >= 5 else None
    snapshot._median_cache[key] = result
    return result


def _sector_day_change(snapshot: MarketSnapshot, sector: str, *, exclude: str) -> float | None:
    key = ("sector_day", sector, exclude)
    if key in snapshot._median_cache:
        return snapshot._median_cache[key]
    values = []
    for symbol, value in snapshot.sector_of.items():
        if value != sector or symbol == exclude:
            continue
        series = snapshot.series.get(symbol)
        if series is not None and len(series) and series.previous_close:
            values.append(series.close[-1] / series.previous_close - 1.0)
    result = statistics.median(values) if len(values) >= 3 else None
    snapshot._median_cache[key] = result
    return result
