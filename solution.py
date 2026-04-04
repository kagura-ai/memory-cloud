def mask_api_key(key: str) -> str:
    """
    Masks an API key by showing only the first four and last four characters,
    with a short ellipsis in between. Example: 'sk-p*********************eFfe' → 'sk-p***eFfe'.
    """
    if not key:
        return ""

    # Ensure we don't reveal keys that are already short
    if len(key) <= 8:
        return key

    prefix = key[:4]
    suffix = key[-4:]
    return f"{prefix}***{suffix}"