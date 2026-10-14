"""Own login, logout, signed sessions and authenticated-page protection.

Admin and viewer share the login, pages and visible controls. Enforce permissions
on the server: reject viewer mutations and keep browsing free of workflow writes.
"""
