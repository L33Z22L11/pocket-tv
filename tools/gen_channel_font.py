"""Bake Pocket TV Channel (Fusion Pixel subset) from a pinned 12px BDF.
Usage: python3 tools/gen_channel_font.py FONT.bdf
Character set is versioned separately; output contains flash-only 1-bit rows.
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
chars=set((ROOT/'assets/fonts/fusion-pixel/characters.txt').read_text())-{'\n'}
glyphs={}
for block in Path(sys.argv[1]).read_text().split('STARTCHAR ')[1:]:
 lines=block.splitlines();fields={line.split(' ',1)[0]:line.split(' ',1)[1] for line in lines if ' ' in line}
 code=int(fields['ENCODING'])
 if code<0 or chr(code) not in chars:continue
 width,height,x,y=map(int,fields['BBX'].split());top=10-height-y
 rows=[0]*12
 for row,raw in enumerate(lines[lines.index('BITMAP')+1:][:height]):
  bits=int(raw,16)<<(16-8*((width+7)//8))
  if 0<=top+row<12:rows[top+row]=(bits>>x) if x>=0 else (bits<<(-x))&65535
 glyphs[code]=(min(12,int(fields['DWIDTH'].split()[0])),rows)
missing=chars-{chr(c) for c in glyphs}
if missing:print(f'Unsupported source glyphs (rendered as dash): {sorted(missing)}')
codes=sorted(glyphs)
output='// Pocket TV Channel: derived from Fusion Pixel 2026.09.01, SIL OFL 1.1.\n#pragma once\n#include <stdint.h>\n'
output+=f'#define CHANNEL_GLYPHS {len(codes)}\n'
output+='static const uint16_t channel_codes[]={'+','.join(map(str,codes))+'};\n'
output+='static const uint8_t channel_widths[]={'+','.join(str(glyphs[c][0]) for c in codes)+'};\n'
output+='static const uint16_t channel_bits[][12]={\n'+',\n'.join('{'+','.join(map(str,glyphs[c][1]))+'}' for c in codes)+'\n};\n'
(ROOT/'firmware/main/channel_font.h').write_text(output)
print(f'{len(codes)} glyphs, {len(codes)*27} flash bytes, zero glyph heap allocation')
