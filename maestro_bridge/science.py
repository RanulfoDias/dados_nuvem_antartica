"""Pure, strict ORTEC ASCII SPE reader; never modifies its input."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import math
import re
from zoneinfo import ZoneInfo

DETECTORS = frozenset({1, 2, 3, 4, 5, 7})


class SpectrumError(ValueError):
    pass


@dataclass(frozen=True)
class Spectrum:
    sha256: str
    detector_id: int
    acquired_at: str
    acquired_local: str
    acquisition_timezone: str
    live_seconds: float
    real_seconds: float
    channel_start: int
    channel_end: int
    counts: list[int]
    total_counts: int
    count_sigma: float
    live_rate_cps: float
    live_rate_sigma_cps: float
    real_rate_cps: float
    real_rate_sigma_cps: float
    sections: dict[str, list[str]]
    encoding: str
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def parse_spe(raw: bytes, *, acquisition_timezone: str = "UTC",
              detector_hint: int | None = None, ambiguous_fold: int | None = None) -> Spectrum:
    """Times are acquisition start, not filesystem timestamps. Rates use MEAS_TIM."""
    if not raw or b"\x00" in raw:
        raise SpectrumError("Empty or non-text SPE")
    try:
        content = raw.decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        try:
            content = raw.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise SpectrumError("Invalid text encoding") from exc
        encoding = "cp1252"
    sections: dict[str, list[str]] = {}
    current = None
    for line in content.splitlines():
        if line.strip().startswith("$"):
            match = re.fullmatch(r"\$([A-Za-z0-9_]+):\s*", line.strip())
            if not match:
                raise SpectrumError("Invalid SPE section header")
            current = match[1].upper()
            if current in sections:
                raise SpectrumError(f"Duplicate section {current}")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        elif line.strip():
            raise SpectrumError("Data before first SPE section")
    for required in ("MEAS_TIM", "DATE_MEA", "DATA"):
        if required not in sections:
            raise SpectrumError(f"Missing {required}")
    try:
        times = " ".join(sections["MEAS_TIM"]).split()
        if len(times) != 2:
            raise ValueError("MEAS_TIM must have live and real time")
        live, real = map(float, times)
        if not all(math.isfinite(t) and t > 0 for t in (live, real)) or live > real:
            raise ValueError("Invalid live/real times")
        date_text = " ".join(sections["DATE_MEA"]).strip()
        local = datetime.strptime(date_text, "%m/%d/%Y %H:%M:%S")
        zone = ZoneInfo(acquisition_timezone)
        candidates = [local.replace(tzinfo=zone, fold=f) for f in (0, 1)]
        valid = [d for d in candidates if d.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == local]
        if not valid:
            raise ValueError("Nonexistent local acquisition time")
        if candidates[0].utcoffset() != candidates[1].utcoffset() and ambiguous_fold not in (0, 1):
            raise ValueError("Ambiguous acquisition time; configure ambiguous_fold")
        acquired = local.replace(tzinfo=zone, fold=ambiguous_fold or 0)
        tokens = " ".join(sections["DATA"]).split()
        if len(tokens) < 2:
            raise ValueError("Missing channel bounds")
        if any(not re.fullmatch(r"\d+", t) for t in tokens):
            raise ValueError("Channel bounds/counts must be nonnegative integers")
        start, end = map(int, tokens[:2])
        if end < start or len(tokens) - 2 != end - start + 1:
            raise ValueError("Truncated or oversized channel data")
        counts = list(map(int, tokens[2:]))
        ids = {int(m[1]) for line in sections.get("SPEC_REM", [])
               if (m := re.match(r"\s*DET#\s*:?\s*(\d+)\s*$", line, re.I))}
        if detector_hint is not None:
            ids.add(detector_hint)
        if len(ids) != 1 or next(iter(ids)) not in DETECTORS:
            raise ValueError("Missing, conflicting or unsupported detector ID")
        detector = next(iter(ids))
        for name in ("MCA_CAL", "SHAPE_CAL", "ENER_FIT"):
            if name not in sections:
                continue
            calibration = " ".join(sections[name]).split()
            if name == "ENER_FIT":
                coefficients = calibration
                if len(coefficients) != 2:
                    raise ValueError("ENER_FIT requires two coefficients")
            else:
                if not calibration or not re.fullmatch(r"\d+", calibration[0]):
                    raise ValueError(f"Invalid {name} coefficient count")
                count = int(calibration[0])
                # MCA_CAL may append an energy unit, e.g. keV.
                allowed = (count+1, count+2) if name == "MCA_CAL" else (count+1,)
                if count < 1 or len(calibration) not in allowed:
                    raise ValueError(f"Incomplete {name} calibration")
                coefficients = calibration[1:count+1]
            if not all(math.isfinite(float(c)) for c in coefficients):
                raise ValueError(f"Nonfinite {name} calibration")
        if "ROI" in sections:
            roi = " ".join(sections["ROI"]).split()
            if not roi or any(not re.fullmatch(r"\d+", t) for t in roi):
                raise ValueError("Invalid ROI")
            values = list(map(int, roi))
            if len(values) != 1 + 2 * values[0]:
                raise ValueError("Incomplete ROI")
            for low, high in zip(values[1::2], values[2::2]):
                if not start <= low <= high <= end:
                    raise ValueError("ROI outside channel bounds")
    except (ValueError, OverflowError) as exc:
        raise SpectrumError(str(exc)) from exc
    total = sum(counts)
    sigma = math.sqrt(total)
    return Spectrum(hashlib.sha256(raw).hexdigest(), detector, acquired.astimezone(timezone.utc).isoformat(),
                    date_text, acquisition_timezone, live, real, start, end, counts, total,
                    sigma, total/live, sigma/live, total/real, sigma/real, sections, encoding)
