"""Shared chat history helpers for web app and RQ worker."""


def retain_only_last_user_images(history: list) -> None:
    """Drop image payloads from older user turns to limit memory and token use."""
    last_user_idx = None
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return
    for i, m in enumerate(history):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        if i == last_user_idx or not m.get("images_b64"):
            continue
        new_m = dict(m)
        new_m.pop("images_b64", None)
        c = new_m.get("content") or ""
        if "[earlier image omitted]" not in c:
            new_m["content"] = c + " [earlier image omitted]"
        history[i] = new_m
