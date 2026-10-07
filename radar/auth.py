"""Small private-dashboard sessions; no wallet or provider credentials in cookies."""
from http.cookies import SimpleCookie, CookieError
import secrets
import threading
import time


def cookies(header):
    try:
        jar = SimpleCookie(header or '')
        return {key:value.value for key,value in jar.items()}
    except CookieError:
        return {}


class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions, self.challenges = {}, {}
        self.failures = []

    def prune(self):
        now = time.time()
        self.sessions = {k:v for k,v in self.sessions.items() if v>now}
        self.challenges = {k:v for k,v in self.challenges.items() if v>now}
        self.failures = [stamp for stamp in self.failures if now-stamp<300]

    def challenge(self):
        with self.lock:
            self.prune()
            if len(self.challenges)>=1000:
                return None
            token = secrets.token_urlsafe(32)
            self.challenges[token] = time.time()+600
            return token

    def valid(self, token):
        with self.lock:
            self.prune()
            return bool(token and token in self.sessions)

    def login(self, nonce, supplied, expected):
        with self.lock:
            self.prune()
            if len(self.failures)>=30:
                return None, 'limited'
            if not nonce or nonce not in self.challenges:
                return None, 'invalid'
            self.challenges.pop(nonce)
            if not secrets.compare_digest(supplied.encode(),expected.encode()):
                self.failures.append(time.time())
                return None, 'invalid'
            if len(self.sessions)>=1000:
                return None, 'limited'
            token = secrets.token_urlsafe(32)
            self.sessions[token] = time.time()+8*3600
            return token, 'ok'

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token,None)
