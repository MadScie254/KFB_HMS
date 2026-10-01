"""Small helpers shared by HTTP view modules."""

def _validation_message(exc):
    if hasattr(exc, "messages"):
        return " ".join(exc.messages)
    return str(exc)
