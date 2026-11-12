"""清点交换字节与伙伴身份。"""
from .io import asset,integer

def summarize(v,root):
    if not isinstance(v['partner'],str) or not v['partner']:raise ValueError('伙伴身份缺失')
    raw,digest=asset(root,v['source']);integer(v['chunk_size'],1)
    if v['charset']!='iso-8859-1':raise ValueError('字符集不在清单范围')
    return {'raw_bytes':len(raw),'sha256':digest,'partner':v['partner'],'has_UNA_prefix':raw.startswith(b'UNA')}
