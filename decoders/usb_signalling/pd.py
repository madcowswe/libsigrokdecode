##
## This file is part of the libsigrokdecode project.
##
## Copyright (C) 2011 Gareth McMullin <gareth@blacksphere.co.nz>
## Copyright (C) 2012-2013 Uwe Hermann <uwe@hermann-uwe.de>
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
##

import sigrokdecode as srd

'''
OUTPUT_PYTHON format:

Packet:
[<ptype>, <pdata>]

<ptype>, <pdata>:
 - 'SOP', None
 - 'SYM', <sym>
 - 'BIT', <bit>
 - 'STUFF BIT', None
 - 'EOP', None
 - 'ERR', None
 - 'KEEP ALIVE', None
 - 'RESET', None

<sym>:
 - 'J', 'K', 'SE0', or 'SE1'

<bit>:
 - '0' or '1'
 - Note: Symbols like SE0, SE1, and the J that's part of EOP don't yield 'BIT'.
'''

# Low-/full-speed symbols.
# Note: Low-speed J and K are inverted compared to the full-speed J and K!
symbols = {
    'low-speed': {
        # (<dp>, <dm>): <symbol/state>
        (0, 0): 'SE0',
        (1, 0): 'K',
        (0, 1): 'J',
        (1, 1): 'SE1',
    },
    'full-speed': {
        # (<dp>, <dm>): <symbol/state>
        (0, 0): 'SE0',
        (1, 0): 'J',
        (0, 1): 'K',
        (1, 1): 'SE1',
    },
}

bitrates = {
    'low-speed': 1500000,   # 1.5Mb/s (+/- 1.5%)
    'full-speed': 12000000, # 12Mb/s (+/- 0.25%)
}

sym_annotation = {
    'J': [0, ['J']],
    'K': [1, ['K']],
    'SE0': [2, ['SE0', '0']],
    'SE1': [3, ['SE1', '1']],
}

class SamplerateError(Exception):
    pass

class Decoder(srd.Decoder):
    api_version = 2
    id = 'usb_signalling'
    name = 'USB signalling'
    longname = 'Universal Serial Bus (LS/FS) signalling'
    desc = 'USB (low-speed and full-speed) signalling protocol.'
    license = 'gplv2+'
    inputs = ['logic']
    outputs = ['usb_signalling']
    channels = (
        {'id': 'dp', 'name': 'D+', 'desc': 'USB D+ signal'},
        {'id': 'dm', 'name': 'D-', 'desc': 'USB D- signal'},
    )
    options = (
        {'id': 'signalling', 'desc': 'Signalling',
            'default': 'full-speed', 'values': ('full-speed', 'low-speed')},
    )
    annotations = (
        ('sym-j', 'J symbol'),
        ('sym-k', 'K symbol'),
        ('sym-se0', 'SE0 symbol'),
        ('sym-se1', 'SE1 symbol'),
        ('sop', 'Start of packet (SOP)'),
        ('eop', 'End of packet (EOP)'),
        ('bit', 'Bit'),
        ('stuffbit', 'Stuff bit'),
        ('error', 'Error'),
        ('keep-alive', 'Low-speed keep-alive'),
        ('reset', 'Reset'),
    )
    annotation_rows = (
        ('bits', 'Bits', (4, 5, 6, 7, 8, 9, 10)),
        ('symbols', 'Symbols', (0, 1, 2, 3)),
    )

    def __init__(self):
        self.samplerate = None
        self.oldsym = 'J' # The "idle" state is J.
        self.ss_block = None
        self.samplenum = 0
        self.bitrate = None
        self.bitwidth = None
        self.samplepos = None
        self.samplenum_target = None
        self.samplenum_edge = None
        self.samplenum_lastedge = 0
        self.oldpins = None
        self.edgepins = None
        self.consecutive_ones = 0
        self.state = 'INIT'

    def start(self):
        self.out_python = self.register(srd.OUTPUT_PYTHON)
        self.out_ann = self.register(srd.OUTPUT_ANN)

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value
            self.bitrate = bitrates[self.options['signalling']]
            self.bitwidth = float(self.samplerate) / float(self.bitrate)

    def putpx(self, data):
        s = self.samplenum_edge
        self.put(s, s, self.out_python, data)

    def putx(self, data):
        s = self.samplenum_edge
        self.put(s, s, self.out_ann, data)

    def putpm(self, data):
        e = self.samplenum_edge
        self.put(self.ss_block, e, self.out_python, data)

    def putm(self, data):
        e = self.samplenum_edge
        self.put(self.ss_block, e, self.out_ann, data)

    def putpb(self, data):
        s, e = self.samplenum_lastedge, self.samplenum_edge
        self.put(s, e, self.out_python, data)

    def putb(self, data):
        s, e = self.samplenum_lastedge, self.samplenum_edge
        self.put(s, e, self.out_ann, data)

    def set_new_target_samplenum(self):
        self.samplepos += self.bitwidth;
        self.samplenum_target = int(self.samplepos)
        self.samplenum_lastedge = self.samplenum_edge
        self.samplenum_edge = int(self.samplepos - (self.bitwidth / 2))

    def wait_for_sop(self, sym):
        # Wait for a Start of Packet (SOP), i.e. a J->K symbol change.
        if sym != 'K' or self.oldsym != 'J':
            return
        self.consecutive_ones = 0
        self.samplepos = self.samplenum - (self.bitwidth / 2) + 0.5
        self.set_new_target_samplenum()
        self.putpx(['SOP', None])
        self.putx([4, ['SOP', 'S']])
        self.state = 'GET BIT'

    def handle_bit(self, b):
        if self.consecutive_ones == 6:
            if b == '0':
                # Stuff bit.
                self.putpb(['STUFF BIT', None])
                self.putb([7, ['Stuff bit: 0', 'SB: 0', '0']])
                self.consecutive_ones = 0
            else:
                self.putpb(['ERR', None])
                self.putb([8, ['Bit stuff error', 'BS ERR', 'B']])
                self.state = 'IDLE'
        else:
            # Normal bit (not a stuff bit).
            self.putpb(['BIT', b])
            self.putb([6, ['%s' % b]])
            if b == '1':
                self.consecutive_ones += 1
            else:
                self.consecutive_ones = 0

    def get_eop(self, sym):
        # EOP: SE0 for >= 1 bittime (usually 2 bittimes), then J.
        self.set_new_target_samplenum()
        self.putpb(['SYM', sym])
        self.putb(sym_annotation[sym])
        self.oldsym = sym
        if sym == 'SE0':
            pass
        elif sym == 'J':
            # Got an EOP.
            self.putpm(['EOP', None])
            self.putm([5, ['EOP', 'E']])
            self.state = 'IDLE'
            self.bitwidth = float(self.samplerate) / float(self.bitrate)
        else:
            self.putpm(['ERR', None])
            self.putm([8, ['EOP Error', 'EErr', 'E']])
            self.state = 'IDLE'

    def get_bit(self, sym):
        self.set_new_target_samplenum()
        if sym == 'SE0':
            # Start of an EOP. Change state, save edge
            self.state = 'GET EOP'
            self.ss_block = self.samplenum_lastedge
        else:
            b = '0' if self.oldsym != sym else '1'
            self.handle_bit(b)
        self.putpb(['SYM', sym])
        self.putb(sym_annotation[sym])
        if self.oldsym != sym:
            edgesym = symbols[self.options['signalling']][tuple(self.edgepins)]
            if edgesym not in ('SE0', 'SE1'):
                if edgesym == sym:
                    self.bitwidth = self.bitwidth - (0.001 * self.bitwidth)
                    self.samplepos = self.samplepos - (0.01 * self.bitwidth)
                else:
                    self.bitwidth = self.bitwidth + (0.001 * self.bitwidth)
                    self.samplepos = self.samplepos + (0.01 * self.bitwidth)
        self.oldsym = sym

    def handle_idle(self, sym):
        self.samplenum_edge = self.samplenum
        se0_length = float(self.samplenum - self.samplenum_lastedge) / self.samplerate
        if se0_length > 2.5e-6: # 2.5us
            self.putpb(['RESET', None])
            self.putb([10, ['Reset', 'Res', 'R']])
        elif se0_length > 1.2e-6 and self.options['signalling'] == 'low-speed':
            self.putpb(['KEEP ALIVE', None])
            self.putb([9, ['Keep-alive', 'KA', 'A']])
        self.state = 'IDLE'

    def decode(self, ss, es, data):
        if not self.samplerate:
            raise SamplerateError('Cannot decode without samplerate.')
        for (self.samplenum, pins) in data:
            # State machine.
            if self.state == 'IDLE':
                # Ignore identical samples early on (for performance reasons).
                if self.oldpins == pins:
                    continue
                self.oldpins = pins
                sym = symbols[self.options['signalling']][tuple(pins)]
                if sym == 'SE0':
                    self.samplenum_lastedge = self.samplenum
                    self.state = 'WAIT IDLE'
                else:
                    self.wait_for_sop(sym)
                self.edgepins = pins
            elif self.state in ('GET BIT', 'GET EOP'):
                # Wait until we're in the middle of the desired bit.
                if self.samplenum == self.samplenum_edge:
                    self.edgepins = pins
                if self.samplenum < self.samplenum_target:
                    continue
                sym = symbols[self.options['signalling']][tuple(pins)]
                if self.state == 'GET BIT':
                    self.get_bit(sym)
                elif self.state == 'GET EOP':
                    self.get_eop(sym)
            elif self.state == 'WAIT IDLE':
                if self.oldpins == pins:
                    continue
                sym = symbols[self.options['signalling']][tuple(pins)]
                if self.samplenum - self.samplenum_lastedge > 1:
                    self.handle_idle(sym)
                else:
                    self.wait_for_sop(sym)
                self.oldpins = pins
                self.edgepins = pins
            elif self.state == 'INIT':
                sym = symbols[self.options['signalling']][tuple(pins)]
                self.handle_idle(sym)
                self.oldpins = pins
