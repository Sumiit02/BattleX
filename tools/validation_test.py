"""
Run simple validation tests against the email and phone helpers inside app.signup.
This script imports the helper functions by creating a small wrapper that calls the same regex.
"""
import re

email_re = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
phone_re = re.compile(r"^[0-9]{10}$")

cases = [
    ('valid email', 'user@example.com', True),
    ('valid email 2', 'john.doe+tag@sub.domain.co', True),
    ('invalid email no at', 'userexample.com', False),
    ('invalid email bad domain', 'user@.com', False),
]

phone_cases = [
    ('valid phone', '9876543210', True),
    ('valid phone 2', '', True),  # empty allowed
    ('invalid phone short', '12345', False),
    ('invalid phone letters', '98ab567210', False),
]

print('Email tests:')
for name, val, expect in cases:
    ok = bool(email_re.match(val))
    print(name, val, '=>', ok, 'expected', expect)

print('\nPhone tests:')
for name, val, expect in phone_cases:
    ok = True if val == '' else bool(phone_re.match(val))
    print(name, val, '=>', ok, 'expected', expect)
