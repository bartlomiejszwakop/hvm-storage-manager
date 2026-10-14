"""Konsola: kolory, symbole stanu (z zapasowym ASCII), ramka nagłówka i pytania do operatora."""
import os
import shlex
import subprocess
import sys


class Cancelled(Exception):
    """Operator wpisał q / 0 (lub zamknął wejście) — powrót do menu."""


_FANCY = {'ok': '✓', 'fail': '✗', 'unknown': '?', 'skip': '○', 'undo': '↺', 'box': '╔═╗║╚╝'}
_ASCII = {'ok': '+', 'fail': 'x', 'unknown': '?', 'skip': 'o', 'undo': '<', 'box': '+-+|++'}
STATE_SYMBOL = {'ok': 'ok', 'error': 'fail', 'unknown': 'unknown'}


def fancy():
    """True, gdy stdout potrafi zakodować symbole ✓✗○↺ i ramki (inaczej zapasowe ASCII)."""
    enc = getattr(sys.stdout, 'encoding', None)
    if not enc:
        return True
    try:
        '✓✗○↺╔═╗║╚╝'.encode(enc)
    except (UnicodeError, LookupError):
        return False
    return True


def sym(key):
    return (_FANCY if fancy() else _ASCII)[key]


def state_line(state, text):
    return f'{sym(STATE_SYMBOL.get(state, "unknown"))} {text}'


def paint(code, text):
    tty = getattr(sys.stdout, 'isatty', lambda: False)()
    return text if os.environ.get('NO_COLOR') or not tty else f'\033[{code}m{text}\033[0m'


def err(msg): print(paint('31', f'BŁĄD: {msg}'))
def ok(msg): print(paint('32', msg))
def warn(msg): print(paint('33', f'UWAGA: {msg}'))


def box(lines):
    tl, h, tr, v, bl, br = sym('box')
    width = max(len(line) for line in lines) + 2
    print(tl + h * width + tr)
    for line in lines:
        print(f'{v} {line.ljust(width - 2)} {v}')
    print(bl + h * width + br)


def describe(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or '').strip()
        cmd = shlex.join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
        return f'polecenie `{cmd}` zakończone kodem {exc.returncode}' + (f': {detail}' if detail else '')
    if isinstance(exc, KeyboardInterrupt):
        return 'przerwano (Ctrl+C)'
    return str(exc) or type(exc).__name__


def ask_yes_no(prompt):
    """Pytanie [t/N]: domyślnie NIE (także przy EOF)."""
    try:
        return input(prompt).strip().lower() in ('t', 'tak', 'y', 'yes')
    except EOFError:
        print()
        return False


def ask_phrase(prompt, phrase):
    """True tylko po wpisaniu dokładnie `phrase`."""
    try:
        return input(prompt).strip() == phrase
    except EOFError:
        print()
        return False


def choose(prompt, valid):
    """Wybór z menu; EOF = '0' (powrót)."""
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            print()
            return '0'
        if raw in valid:
            return raw
        print('Nieznana opcja.')


def ask(prompt, default=None, convert=str):
    """Pyta do skutku: Enter = domyślna, q = anuluj, błąd walidacji = komunikat i ponowne pytanie."""
    hint = f' [{default}]' if default is not None else ''
    while True:
        try:
            raw = input(f'{prompt}{hint}: ').strip()
        except EOFError:
            raise Cancelled
        if raw.lower() == 'q':
            raise Cancelled
        try:
            return convert(raw or ('' if default is None else str(default)))
        except ValueError as exc:
            err(str(exc))
