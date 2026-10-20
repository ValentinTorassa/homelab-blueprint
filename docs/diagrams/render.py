#!/usr/bin/env python3
"""Render the diagrams in the READMEs as SVG: a light and a dark file for each.

    python3 docs/diagrams/render.py           # the diagrams
    python3 docs/diagrams/render.py --demo    # also re-record demo.svg from backups/demo.sh

The READMEs embed them with <picture>, so GitHub shows the file that matches the
reader's theme. Change a diagram here and re-run; never edit the SVG files by
hand. Text widths are estimated, so look at the result in a browser after a
change. Standard library only.
"""
import argparse
import html
import math
import os
import re
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]

SANS = '-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif'
MONO = 'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace'

# GitHub's own palette, so the diagrams sit on the page like native content.
# mon/bak/drf/sby are the four pillars; bad, warn and ink are for states and
# neutral arrows.
THEMES = {
    'light': dict(bg='#ffffff', fg='#1f2328', muted='#59636e', faint='#afb8c1',
                  node='#ffffff', line='#d1d9e0', panel='#f6f8fa', chip='#eff2f5',
                  badge='#ffffff', mon='#0969da', bak='#1a7f37', drf='#8250df',
                  sby='#bc4c00', bad='#cf222e', warn='#9a6700', ink='#59636e'),
    'dark': dict(bg='#0d1117', fg='#f0f6fc', muted='#9198a1', faint='#3d444d',
                 node='#212830', line='#3d444d', panel='#151b23', chip='#2a313c',
                 badge='#0d1117', mon='#4493f8', bak='#3fb950', drf='#ab7df8',
                 sby='#f0883e', bad='#f85149', warn='#d29922', ink='#9198a1'),
}
ACCENTS = ('mon', 'bak', 'drf', 'sby', 'bad', 'warn', 'ink')

STYLE = """
text{font-family:@sans;fill:@fg}
.mono{font-family:@mono}
.t{font-size:14px;font-weight:600}
.s{font-size:12px;fill:@muted}
.l{font-size:12px;font-weight:500}
.cap{font-size:11px;font-weight:600;letter-spacing:.08em;fill:@muted}
.chipt{font-size:11px;fill:@muted}
.panel{fill:@panel;stroke:@line}
.node{fill:@node;stroke:@line;stroke-width:1.25}
.chip{fill:@chip}
.icon{fill:none;stroke:@muted;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.e{fill:none;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
.dash{stroke-dasharray:5 4}
.life{stroke:@faint;stroke-width:1.25;stroke-dasharray:3 4}
.badge{font-size:10px;font-weight:700;fill:@badge}
"""


def esc(text):
    return html.escape(text, quote=True)


def tw(text, size=12, mono=False, bold=False):
    """Estimated rendered width of a string, for layout only."""
    if mono:
        return 0.6 * size * len(text)
    w = 0.0
    for ch in text:
        if ch in 'iljtf.,:;\'|!()[] ':
            w += 0.30
        elif ch in 'mwMW':
            w += 0.85
        elif ch.isupper() or ch in '0123456789':
            w += 0.64
        else:
            w += 0.55
    return w * size * (1.06 if bold else 1.0)


def fmt(v):
    return f'{v:.1f}'.rstrip('0').rstrip('.')


def rounded(points, r=8):
    """SVG path through the points, with rounded corners."""
    d = [f'M{fmt(points[0][0])},{fmt(points[0][1])}']
    for i in range(1, len(points) - 1):
        (ax, ay), (bx, by), (cx, cy) = points[i - 1], points[i], points[i + 1]
        la, lc = math.hypot(ax - bx, ay - by), math.hypot(cx - bx, cy - by)
        k = min(r, la / 2, lc / 2)
        p1 = (bx + (ax - bx) * k / la, by + (ay - by) * k / la)
        p2 = (bx + (cx - bx) * k / lc, by + (cy - by) * k / lc)
        d.append(f'L{fmt(p1[0])},{fmt(p1[1])} Q{fmt(bx)},{fmt(by)} {fmt(p2[0])},{fmt(p2[1])}')
    d.append(f'L{fmt(points[-1][0])},{fmt(points[-1][1])}')
    return ' '.join(d)


class Svg:
    def __init__(self, w, h, title, desc):
        self.w, self.h, self.title, self.desc = w, h, title, desc
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    # -- primitives ---------------------------------------------------------------

    def text(self, x, y, s, cls='l', accent=None, anchor='middle', mono=False, weight=None):
        classes = cls + (' mono' if mono else '')
        # Inline style, not attributes: the stylesheet would override fill= and font-*=.
        style = (f'fill:@{accent};' if accent else '') + (f'font-weight:{weight};' if weight else '')
        style = f' style="{style}"' if style else ''
        self.add(f'<text x="{fmt(x)}" y="{fmt(y)}" class="{classes}" text-anchor="{anchor}"{style}>'
                 f'{esc(s)}</text>')

    def edge(self, points, accent, dashed=False, start=False, end=True):
        cls = 'e dash' if dashed else 'e'
        m = ''
        if end:
            m += f' marker-end="url(#m-{accent})"'
        if start:
            m += f' marker-start="url(#m-{accent})"'
        self.add(f'<path d="{rounded(points)}" class="{cls}" stroke="@{accent}"{m}/>')

    def icon(self, kind, cx, cy):
        x, y = cx, cy
        if kind == 'server':
            s = (f'<rect x="{x-9}" y="{y-9}" width="18" height="8" rx="2"/>'
                 f'<rect x="{x-9}" y="{y+1}" width="18" height="8" rx="2"/>'
                 f'<path d="M{x-5},{y-5}h.01M{x-5},{y+5}h.01"/>')
        elif kind == 'disk':
            s = (f'<ellipse cx="{x}" cy="{y-6}" rx="8" ry="3"/>'
                 f'<path d="M{x-8},{y-6}v12a8,3 0 0 0 16,0v-12M{x-8},{y}a8,3 0 0 0 16,0"/>')
        elif kind == 'pulse':
            s = f'<path d="M{x-10},{y}h4l2.5,-7l4,14l2.5,-7h7"/>'
        elif kind == 'bucket':
            s = (f'<ellipse cx="{x}" cy="{y-6}" rx="8" ry="2.5"/>'
                 f'<path d="M{x-8},{y-6}l2,13a6,2 0 0 0 12,0l2,-13"/>')
        elif kind == 'power':
            s = (f'<rect x="{x-10}" y="{y-10}" width="20" height="20" rx="4"/>'
                 f'<path d="M{x-3.5},{y-2.5}a5,5 0 1 0 7,0M{x},{y-5.5}v4.5"/>')
        elif kind == 'lambda':
            s = f'<path d="M{x-6},{y-8}h3l9,16M{x-0.5},{y-1}l-5.5,9"/>'
        elif kind == 'git':
            s = (f'<circle cx="{x-5}" cy="{y-7}" r="2.2"/><circle cx="{x-5}" cy="{y+7}" r="2.2"/>'
                 f'<circle cx="{x+5}" cy="{y-4}" r="2.2"/>'
                 f'<path d="M{x-5},{y-4.8}v9.6M{x+5},{y-1.8}c0,4 -10,3 -10,6.6"/>')
        elif kind == 'person':
            s = (f'<circle cx="{x}" cy="{y-4}" r="3.5"/>'
                 f'<path d="M{x-7},{y+8}c0,-7 14,-7 14,0"/>')
        elif kind == 'users':
            s = (f'<circle cx="{x-3}" cy="{y-4}" r="3.2"/><path d="M{x-9.5},{y+8}c0,-6.5 13,-6.5 13,0"/>'
                 f'<path d="M{x+3.5},{y-7.5}a3.2,3.2 0 0 1 0,6.4M{x+6},{y+1.5}c2.2,1 3.5,3 3.5,6.5"/>')
        else:
            raise ValueError(kind)
        self.add(f'<g class="icon">{s}</g>')

    # -- components ---------------------------------------------------------------

    def panel(self, x, y, w, h, caption):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" class="panel"/>')
        self.text(x + 16, y + 21, caption, cls='cap', anchor='start')

    def node(self, x, y, w, h, title, sub, icon, accent=None):
        stroke = f' style="stroke:@{accent}"' if accent else ''
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="node"{stroke}/>')
        self.icon(icon, x + 26, y + h / 2)
        self.text(x + 46, y + h / 2 - 3, title, cls='t', anchor='start')
        self.text(x + 46, y + h / 2 + 14, sub, cls='s', anchor='start')

    def pill(self, cx, cy, w, h, label, icon):
        x = cx - w / 2
        self.add(f'<rect x="{fmt(x)}" y="{fmt(cy - h / 2)}" width="{w}" height="{h}" rx="{h / 2}" class="node"/>')
        self.icon(icon, x + 24, cy)
        self.text(x + 42, cy + 5, label, cls='t', anchor='start')

    def chips(self, cx, y, labels):
        widths = [tw(t, 11) + 18 for t in labels]
        x = cx - (sum(widths) + 8 * (len(labels) - 1)) / 2
        for t, w in zip(labels, widths):
            self.add(f'<rect x="{fmt(x)}" y="{y}" width="{fmt(w)}" height="20" rx="10" class="chip"/>')
            self.text(x + w / 2, y + 14, t, cls='chipt')
            x += w + 8

    def legend(self, cx, y, items):
        """items: (accent, label, dashed). Centred on cx."""
        widths = [30 + tw(label, 12) for _, label, _ in items]
        x = cx - (sum(widths) + 22 * (len(items) - 1)) / 2
        for (accent, label, dashed), w in zip(items, widths):
            cls = 'e dash' if dashed else 'e'
            self.add(f'<path d="M{fmt(x)},{y - 4}h22" class="{cls}" stroke="@{accent}"/>')
            self.text(x + 30, y, label, cls='s', anchor='start')
            x += w + 22

    def badge(self, cx, cy, n, accent):
        self.add(f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="8.5" fill="@{accent}"/>')
        self.text(cx, cy + 3.5, str(n), cls='badge')

    # -- output -------------------------------------------------------------------

    def render(self, theme):
        t = THEMES[theme]
        css = STYLE.replace('@sans', SANS).replace('@mono', MONO)
        markers = ''.join(
            f'<marker id="m-{a}" viewBox="0 0 10 10" refX="8.6" refY="5" markerWidth="6.5" '
            f'markerHeight="6.5" orient="auto-start-reverse"><path d="M1,1L9,5L1,9z" fill="@{a}"/></marker>'
            for a in ACCENTS)
        body = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
                f'width="{self.w}" height="{self.h}" role="img" aria-labelledby="title desc">\n'
                f'<title id="title">{esc(self.title)}</title>\n<desc id="desc">{esc(self.desc)}</desc>\n'
                f'<style>{css}</style>\n<defs>{markers}</defs>\n' + '\n'.join(self.parts) + '\n</svg>\n')
        # Longest names first, so @line never eats the start of a longer token.
        for key in sorted(t, key=len, reverse=True):
            body = body.replace(f'@{key}', t[key])
        return body


# -- the diagrams -------------------------------------------------------------------

def architecture():
    s = Svg(880, 584, 'homelab-blueprint architecture',
            'The home server snapshots to a local disk and ships offsite copies to a versioned S3 '
            'bucket. Jobs push results to Gatus on separate hardware, Gatus probes the server, and '
            'sends alerts and a signed heartbeat to a relay in the cloud that messages the operator. '
            'A nightly drift check compares installed files with GitHub. The cold standby in AWS '
            'reads the bucket with a read-only role and is started by the operator.')
    s.node(345, 16, 190, 56, 'GitHub', 'source of truth', 'git')
    s.panel(16, 100, 848, 136, 'HOME NETWORK')
    s.node(40, 136, 190, 72, 'backup disk', 'local snapshots', 'disk')
    s.node(345, 136, 190, 72, 'homeserver', 'services and jobs', 'server')
    s.node(650, 136, 190, 72, 'monitor', 'Gatus, own hardware', 'pulse')
    s.panel(16, 292, 848, 136, 'AWS')
    s.node(40, 328, 190, 72, 'cold standby', 'EC2 arm64, stopped', 'power')
    s.node(345, 328, 190, 72, 'S3 bucket', 'versioned, Object Lock', 'bucket')
    s.node(650, 328, 190, 72, 'alert relay', 'serverless function', 'lambda')
    s.pill(440, 484, 124, 40, 'operator', 'person')

    s.edge([(440, 136), (440, 72)], 'drf')
    s.text(452, 91, 'nightly drift check, by blob SHA', accent='drf', anchor='start')
    s.edge([(345, 172), (230, 172)], 'bak')
    s.text(287.5, 164, 'nightly snapshots', accent='bak')
    s.edge([(535, 161), (650, 161)], 'mon')
    s.text(592.5, 153, 'job results (push)', accent='mon')
    s.edge([(650, 184), (535, 184)], 'mon')
    s.text(592.5, 201, 'probes (pull)', accent='mon')
    s.edge([(440, 208), (440, 328)], 'bak')
    s.text(452, 268, 'offsite, daily or weekly', accent='bak', anchor='start')
    s.edge([(725, 208), (725, 328)], 'mon')
    s.text(717, 268, 'alerts', accent='mon', anchor='end')
    s.edge([(765, 208), (765, 328)], 'mon')
    s.text(773, 268, 'heartbeat', accent='mon', anchor='start')
    s.edge([(345, 364), (230, 364)], 'sby', dashed=True)
    s.text(287.5, 356, 'read-only role', accent='sby')
    s.edge([(745, 400), (745, 484), (502, 484)], 'mon')
    s.text(623, 476, 'chat message', accent='mon')
    s.edge([(378, 484), (135, 484), (135, 400)], 'sby', dashed=True)
    s.text(256, 476, 'decides, starts, takes over', accent='sby')

    s.legend(440, 552, [('mon', 'monitoring', False), ('bak', 'backups', False),
                        ('drf', 'drift', False), ('sby', 'only in a drill or a takeover', True)])
    return s


def failure_path():
    s = Svg(880, 556, 'How a failed nightly backup reaches the operator',
            'cron starts run-and-report.sh at 02:30, which runs backup_snapshot.sh. The job exits 1 '
            'with DUMP_CMD failed on stderr. The wrapper pushes success=false with that error to the '
            'Gatus external endpoint, returns exit 1 to cron, Gatus fires its alert through the relay, '
            'and the operator gets a chat message naming the job and the error.')
    lanes = [('cron', 56, 84, False), ('run-and-report.sh', 200, 150, True),
             ('backup_snapshot.sh', 368, 152, True), ('Gatus', 548, 96, False),
             ('alert relay', 690, 112, False), ('operator', 816, 96, False)]
    for name, x, w, mono in lanes:
        bottom = 476 if name == 'operator' else 456
        s.add(f'<path d="M{x},52V{bottom}" class="life"/>')
        s.add(f'<rect x="{fmt(x - w / 2)}" y="16" width="{w}" height="36" rx="8" class="node"/>')
        s.text(x, 39, name, cls='t' if not mono else 'l', mono=mono, weight='600' if mono else None)
    for lane, top, bottom in ((1, 92, 340), (2, 140, 208), (3, 276, 392), (4, 376, 444)):
        x = lanes[lane][1]
        s.add(f'<rect x="{x - 5}" y="{top}" width="10" height="{bottom - top}" rx="2" class="node"/>')

    messages = [
        (96, 0, 1, ['02:30, backup-app-a'], 'ink', False, False),
        (148, 1, 2, ['run it, capture stderr'], 'ink', False, False),
        (200, 2, 1, ['exit 1: DUMP_CMD failed'], 'bad', True, False),
        (280, 1, 3, ['POST …/endpoints/jobs_backup-app-a/external',
                     'success=false  error="exit 1: … DUMP_CMD failed"'], 'mon', False, True),
        (332, 1, 0, ['exit code kept: 1'], 'bad', True, False),
        (384, 3, 4, ['failure alert'], 'mon', False, False),
        (436, 4, 5, ['chat message'], 'mon', False, False),
    ]
    for n, (y, a, b, lines, accent, dashed, mono) in enumerate(messages, 1):
        xa, xb = lanes[a][1], lanes[b][1]
        d = 1 if xb > xa else -1
        s.edge([(xa + 5 * d, y), (xb - 5 * d, y)], accent, dashed=dashed)
        mid = (xa + 31 * d + xb) / 2
        size = 11.5 if mono else 12
        for i, t in enumerate(lines):
            ly = y - 10 - 15 * (len(lines) - 1 - i)
            s.add(f'<text x="{fmt(mid)}" y="{fmt(ly)}" class="l{" mono" if mono else ""}" '
                  f'text-anchor="middle" style="fill:@{accent};font-size:{size}px">{esc(t)}</text>')
        s.badge(xa + 18 * d, y, n, accent)

    # What lands in the chat.
    s.add('<rect x="520" y="476" width="344" height="64" rx="10" class="node"/>')
    s.add('<rect x="520" y="476" width="5" height="64" rx="2" fill="@bad"/>')
    s.add('<circle cx="542" cy="497" r="4.5" fill="@bad"/>')
    s.add('<text x="554" y="502" class="t">Jobs / backup-app-a'
          '<tspan dx="8" style="fill:@bad">TRIGGERED</tspan></text>')
    s.text(554, 526, 'exit 1: … DUMP_CMD failed', cls='s', anchor='start', mono=True)
    s.text(504, 512, 'what lands in the chat →', cls='s', anchor='end')
    return s


def standby():
    s = Svg(880, 340, 'Cold standby in AWS',
            'The home server ships small families and the gpg secrets bundle daily and large families '
            'weekly to a versioned S3 bucket with Object Lock, which the uploader cannot delete from. '
            'The stopped standby reads the bucket through a read-only instance role. The operator '
            'decides, starts it and runs takeover.sh --primary-is-down; only then does the standby '
            'serve users through the same tunnel.')
    s.pill(765, 36, 124, 40, 'operator', 'person')
    s.node(16, 132, 180, 72, 'homeserver', 'the primary', 'server')
    s.node(345, 132, 190, 72, 'S3 bucket', 'offsite copies', 'bucket')
    s.node(670, 132, 190, 72, 'cold standby', 'EC2 arm64, stopped', 'power')
    s.chips(440, 214, ['versioned', 'Object Lock'])
    s.chips(440, 240, ['the uploader cannot delete'])
    s.pill(765, 304, 108, 40, 'users', 'users')

    s.edge([(196, 168), (345, 168)], 'bak')
    s.text(270.5, 144, 'daily: small families', accent='bak')
    s.text(270.5, 159, 'and the secrets bundle', accent='bak')
    s.text(270.5, 186, 'weekly: large families', accent='bak')
    s.edge([(535, 168), (670, 168)], 'sby', dashed=True)
    s.text(602.5, 159, 'read-only', accent='sby')
    s.text(602.5, 186, 'instance role', accent='sby')
    s.edge([(765, 56), (765, 132)], 'sby', dashed=True)
    s.text(753, 80, '1. decides', accent='sby', anchor='end')
    s.text(753, 96, '2. starts', accent='sby', anchor='end')
    s.add('<text x="753" y="112" class="l" text-anchor="end" style="fill:@sby">3. '
          '<tspan class="mono" style="font-size:11.5px">takeover.sh --primary-is-down</tspan></text>')
    s.edge([(765, 204), (765, 284)], 'sby', dashed=True)
    s.text(753, 248, 'same tunnel, only after the takeover', accent='sby', anchor='end')
    s.legend(190, 308, [('bak', 'backups', False), ('sby', 'only in a drill or a takeover', True)])
    return s


def watchdog():
    s = Svg(760, 196, 'Dead man\'s switch for the monitor',
            'The relay checks the age of the monitor\'s last signed heartbeat on its own schedule. '
            'Healthy moves to warning when beats stop, and to critical when they are still missing. '
            'When a heartbeat arrives, it sends exactly one recovery message and returns to healthy.')
    states = [(40, 'healthy', 'beats arriving', 'bak'), (300, 'warning', 'one message', 'warn'),
              (560, 'critical', 'one message', 'bad')]
    for x, title, sub, accent in states:
        s.add(f'<rect x="{x}" y="40" width="160" height="56" rx="12" class="node" '
              f'style="stroke:@{accent};stroke-width:1.75"/>')
        s.add(f'<circle cx="{x + 22}" cy="62" r="5" fill="@{accent}"/>')
        s.text(x + 36, 66, title, cls='t', accent=accent, anchor='start')
        s.text(x + 36, 84, sub, cls='s', anchor='start')
    s.edge([(200, 68), (300, 68)], 'warn')
    s.text(250, 60, 'beats stop', accent='warn')
    s.edge([(460, 68), (560, 68)], 'bad')
    s.text(510, 60, 'still missing', accent='bad')
    s.edge([(640, 96), (640, 140), (120, 140), (120, 96)], 'bak')
    s.edge([(380, 96), (380, 140)], 'bak', end=False)
    s.text(380, 164, 'a heartbeat arrives: exactly one recovery message', accent='bak')
    s.text(380, 186, 'the relay checks the age of the last signed beat on its own schedule',
           cls='s')
    return s


DIAGRAMS = {'architecture': architecture, 'failure-path': failure_path,
            'standby': standby, 'watchdog': watchdog}


# -- the demo recording ---------------------------------------------------------------

TERM = dict(bg='#0d1117', bar='#161b22', line='#30363d', fg='#e6edf3', muted='#8b949e',
            green='#3fb950', red='#f85149', yellow='#d29922', head='#f0f6fc')
BOLD = ' font-weight="700"'


def demo():
    """Run backups/demo.sh and draw its real output as an animated terminal."""
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    out = subprocess.run(['bash', '-c', 'bash backups/demo.sh 2>&1'], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    lines = ['$ make demo'] + out.rstrip('\n').splitlines()
    lh, top, size = 18, 34 + 22, 11.5
    w = 920
    h = top + lh * len(lines) + 4
    rows, section, delay = [], 0, 0.3
    for i, raw in enumerate(lines):
        if raw.startswith('== '):
            section += 1
            delay += 0.7
        elif i:
            delay += 0.06
        y = top + lh * i
        spans = color_line(raw)
        if '| FAIL' in raw:
            rows.append(f'<rect x="10" y="{y - 13}" width="{w - 20}" height="{lh}" rx="3" '
                        f'fill="{TERM["red"]}" fill-opacity=".14" class="ln" style="animation-delay:{delay:.2f}s"/>')
        tspans = ''.join(f'<tspan fill="{TERM[c]}"{BOLD if bold else ""}>{esc(t)}</tspan>'
                         for t, c, bold in spans)
        rows.append(f'<text x="16" y="{y}" xml:space="preserve" class="ln" '
                    f'style="animation-delay:{delay:.2f}s">{tspans}</text>')
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-labelledby="title desc">
<title id="title">make demo</title>
<desc id="desc">Recorded output of make demo: two apps are snapshotted, the weekly restore test passes, one byte is appended to a file inside the newest backup, and the restore test fails app-a with "SHA256SUMS: 1 file(s) do not match".</desc>
<style>
text{{font-family:{MONO};font-size:{size}px;fill:{TERM["fg"]}}}
.bar{{font-family:{SANS};font-size:12px}}
.ln{{animation:in .25s ease-out backwards}}
@keyframes in{{from{{opacity:0}}}}
@media (prefers-reduced-motion:reduce){{.ln{{animation:none}}}}
</style>
<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="10" fill="{TERM["bg"]}" stroke="{TERM["line"]}"/>
<path d="M.5,34V10.5a10,10 0 0 1 10,-10H{w - 10.5}a10,10 0 0 1 10,10V34z" fill="{TERM["bar"]}"/>
<path d="M.5,34H{w - .5}" stroke="{TERM["line"]}"/>
<circle cx="20" cy="17" r="6" fill="#ff5f57"/><circle cx="40" cy="17" r="6" fill="#febc2e"/><circle cx="60" cy="17" r="6" fill="#28c840"/>
<text x="{w / 2}" y="21" text-anchor="middle" fill="{TERM["muted"]}" class="bar">make demo · homelab-blueprint</text>
{chr(10).join(rows)}
</svg>
'''
    (OUT / 'demo.svg').write_text(svg)


def color_line(line):
    """(text, colour, bold) spans for one line of demo output."""
    if line.startswith('$ '):
        return [('$ ', 'green', True), (line[2:], 'head', True)]
    if line.startswith('== '):
        return [(line, 'head', True)]
    if '| FAIL' in line:
        return [(line, 'red', True)]
    m = re.match(r'(\[[^\]]+\])(.*)', line)
    if m:
        return [(m.group(1), 'muted', False), (m.group(2), 'fg', False)]
    m = re.match(r'(pass|fail|unknown)(\s+\S+\s+\S+\s+)(.*)', line)
    if m:
        colour = {'pass': 'green', 'fail': 'red', 'unknown': 'yellow'}[m.group(1)]
        return [(m.group(1), colour, True), (m.group(2), 'fg', False),
                (m.group(3), colour if colour != 'green' else 'fg', False)]
    if line.startswith('        '):
        return [(line, 'muted', False)]
    if line.startswith('restore test exit code'):
        return [(line, 'yellow', False)]
    return [(line, 'fg', False)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--demo', action='store_true', help='also re-record demo.svg from backups/demo.sh')
    args = ap.parse_args()
    for name, build in DIAGRAMS.items():
        diagram = build()
        for theme in THEMES:
            (OUT / f'{name}-{theme}.svg').write_text(diagram.render(theme))
    if args.demo:
        demo()


if __name__ == '__main__':
    main()
