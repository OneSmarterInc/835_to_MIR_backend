"""Consistent, client-timezone date periods for the FileX Client User portal."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


class InvalidPeriod(ValueError):
    pass


def resolve_period(query, client, *, default="daily", today=None):
    mode = str(query.get("period") or default).strip().lower()
    if mode not in {"daily", "weekly", "monthly", "all", "custom"}:
        raise InvalidPeriod("Choose Daily, Weekly, Monthly, All or Custom.")
    try:
        zone = ZoneInfo(getattr(client, "timezone", "") or "America/New_York")
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("America/New_York")
    business_today = today or timezone.localtime(timezone.now(), zone).date()

    if mode == "all":
        first = last = None
    elif mode == "daily":
        # Backwards-compatible single-date reports when no explicit period
        # is supplied. Normal Client User dashboard always sends period=daily.
        legacy = query.get("date") if not query.get("period") else None
        try:
            first = last = date.fromisoformat(legacy) if legacy else business_today
        except (ValueError, TypeError):
            raise InvalidPeriod("Invalid date. Use YYYY-MM-DD.")
    elif mode == "weekly":
        # Python weekday Monday=0/Sunday=6. Current week starts Sunday.
        first = business_today - timedelta(days=(business_today.weekday() + 1) % 7)
        last = first + timedelta(days=6)
    elif mode == "monthly":
        first = business_today.replace(day=1)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = next_month - timedelta(days=1)
    else:
        try:
            first = date.fromisoformat(str(query.get("from") or ""))
            last = date.fromisoformat(str(query.get("to") or ""))
        except ValueError:
            raise InvalidPeriod("Custom dates require a valid From and To (YYYY-MM-DD).")
        if first > last:
            raise InvalidPeriod("From date must be on or before To date.")
        if last == date.max:
            raise InvalidPeriod("To date must be earlier than 9999-12-31.")

    start = timezone.make_aware(datetime.combine(first, time.min), zone) if first else None
    end = timezone.make_aware(
        datetime.combine(last + timedelta(days=1), time.min), zone
    ) if last else None
    return {
        "mode": mode, "from": first, "to": last,
        "start": start, "end": end, "today": business_today,
        "timezone": str(zone),
    }


def apply_period(queryset, field, period):
    if period["start"] is None:
        return queryset
    return queryset.filter(**{
        field + "__gte": period["start"],
        field + "__lt": period["end"],
    })


def period_payload(period):
    return {
        "mode": period["mode"],
        "from": period["from"].isoformat() if period["from"] else None,
        "to": period["to"].isoformat() if period["to"] else None,
        "today": period["today"].isoformat(),
        "timezone": period["timezone"],
    }
