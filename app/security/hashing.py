import bcrypt

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


# Fixed valid bcrypt hash (default cost) used to equalise login timing when an
# account does not exist, so a "no such user" response takes as long as a real
# "wrong password" one and cannot be distinguished by timing.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalisation-placeholder", bcrypt.gensalt()).decode('utf-8')


def dummy_verify() -> None:
    """Run a bcrypt check against a throwaway hash to burn ~one verify's worth of time."""
    try:
        bcrypt.checkpw(b"invalid", _DUMMY_HASH.encode('utf-8'))
    except Exception:
        pass