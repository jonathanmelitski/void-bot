import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PENN_ID_RE = re.compile(r"^\d{8}$")


class ValidationError(Exception):
    pass


def clean_name(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{label} can't be blank.")
    return value


def clean_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise ValidationError(f"`{value}` doesn't look like a valid email.")
    return value


def clean_penn_id(value: str) -> str:
    value = value.strip()
    if not PENN_ID_RE.match(value):
        raise ValidationError(f"Penn ID `{value}` must be exactly 8 digits.")
    return value


def clean_phone(value: str) -> str:
    """Normalize to digits, keeping a leading + for international numbers."""
    digits = re.sub(r"\D", "", value)
    if not 10 <= len(digits) <= 15:
        raise ValidationError(f"Phone number `{value}` must have 10–15 digits.")
    return ("+" if value.strip().startswith("+") else "") + digits


def format_phone(phone: str | None) -> str | None:
    if phone and len(phone) == 10 and phone.isdigit():
        return f"({phone[:3]}) {phone[3:6]}-{phone[6:]}"
    return phone


CLEANERS = {
    "first_name": lambda v: clean_name(v, "First name"),
    "last_name": lambda v: clean_name(v, "Last name"),
    "email": clean_email,
    "penn_id": clean_penn_id,
    "phone": clean_phone,
    "nickname": lambda v: clean_name(v, "Nickname"),
}


def clean_field(field: str, value: str) -> str:
    return CLEANERS[field](value)


def clean_fields(**fields: str | None) -> dict:
    """Validate whichever fields were given; None ones are left out of the result."""
    return {name: clean_field(name, value) for name, value in fields.items() if value is not None}
