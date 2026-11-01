"""Safe configuration example; never used as runtime authentication settings."""

SECRET_KEY = ""  # Set a private random secret in auth_config.py when configuring auth.

USERS = {
    "example_admin": {
        "password_hash": "dummy-password-hash",
        "role": "admin",
    },
    "example_viewer": {
        "password_hash": "dummy-password-hash",
        "role": "viewer",
    },
}
