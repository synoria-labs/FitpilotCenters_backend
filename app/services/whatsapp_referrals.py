"""Keep bounded Click-to-WhatsApp attribution, never instructions or remote media."""

_FIELDS = {"source_id": 120, "source_type": 30, "source_url": 1024,
           "headline": 500, "body": 2000, "ctwa_clid": 512}


def normalize_referral(value):
    if not isinstance(value, dict):
        return None
    result = {key: value[key].strip()[:limit] for key, limit in _FIELDS.items()
              if isinstance(value.get(key), str) and value[key].strip()}
    return result or None
