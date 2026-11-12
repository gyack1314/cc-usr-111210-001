"""显示清单身份和原始来源概况。"""
import argparse,json
from pathlib import Path
from .io import load
from .ingest import jsonable
from .domain import summarize
from .collect import collect as collect_source

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['inspect','collect']);p.add_argument('file');a=p.parse_args()
    v,digest=load(a.file)
    if a.command=='collect':
        print(json.dumps(jsonable(collect_source(a.file)),ensure_ascii=False,indent=2,allow_nan=False));return
    print(json.dumps({'source_sha256':digest,'summary':summarize(v,Path(a.file).parent)},ensure_ascii=False,indent=2,allow_nan=False))
if __name__=='__main__':main()
