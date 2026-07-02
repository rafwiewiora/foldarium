"""Regenerate quiz display files to show each AF3 pose inside its OWN AF3-predicted protein (no clash
artifact from overlaying on the crystal), hydrogens stripped. All 5 models are superposed onto the
ref (model-1) pocket chain so poses are comparable in one frame. Keeps filenames + quiz_items.json
labels unchanged. Crystal is no longer displayed (scoring is from CAMEO RMSDs, already in the json)."""
import json, warnings, sys
from pathlib import Path
import numpy as np, gemmi
warnings.filterwarnings("ignore")
sys.path.insert(0,"."); sys.path.insert(0,"../viewer")
import process_cameo as P
HERE=Path(".").resolve(); DATA=HERE/"data"; POCKET_R=5.0

def pocket_chain(m, ligpos):
    best=None; bestn=-1
    for ch in m:
        poly=ch.get_polymer()
        if len(poly)<5: continue
        ca=np.array([[a.pos.x,a.pos.y,a.pos.z] for r in poly for a in r if a.name=="CA"])
        if not len(ca): continue
        n=int((np.min(np.linalg.norm(ca[:,None]-ligpos[None],axis=2),axis=1)<10).sum())
        if n>bestn: bestn=n; best=ch.get_polymer()
    return best

def lig_res(m, chain, resname):
    for ch in m:
        if ch.name!=chain: continue
        for r in ch:
            if r.name==resname: return r
    return None

def write_lig(res, tr, dest):
    out=[]; i=0
    for a in res:
        if a.element.name=="H": continue
        i+=1; p=tr.apply(a.pos) if tr else a.pos
        out.append(f"HETATM{i:>5d} {a.name[:4]:<4s} LIG X   1    {p.x:8.3f}{p.y:8.3f}{p.z:8.3f}  1.00  0.00          {a.element.name:>2s}")
    out.append("END"); dest.write_text("\n".join(out)+"\n")

def write_prot(poly, tr, dest, near=None):
    out=[]; i=0
    for r in poly:
        atoms=[(a, (tr.apply(a.pos) if tr else a.pos)) for a in r if a.element.name!="H"]
        if near is not None:
            xyz=np.array([[p.x,p.y,p.z] for _,p in atoms])
            if not len(xyz) or np.min(np.linalg.norm(xyz[:,None]-near[None],axis=2))>=POCKET_R: continue
        for a,p in atoms:
            i+=1
            out.append(f"ATOM  {i:>5d} {a.name[:4]:<4s} {r.name[:3]:>3s} A{r.seqid.num:>4d}    {p.x:8.3f}{p.y:8.3f}{p.z:8.3f}  1.00  0.00          {a.element.name:>2s}")
    out.append("END"); dest.write_text("\n".join(out)+"\n")

def main():
    d=json.load(open("quiz_items.json")); fixed=0; skipped=[]
    for it in d["items"]:
        tgt=it["id"]; week=it["week"]; het=it["ligand"]
        amap=P.af3_ligand_map(week,tgt,het)
        models={}; ligs={}; pock={}
        for c in it["choices"]:
            mdl=c["af3_sample"]; cr=amap.get(mdl)
            if not cr: continue
            cif=P.server_dir(week,tgt)/f"model-{mdl}"/f"model-{mdl}.cif"
            try: st=gemmi.read_structure(str(cif)); st.setup_entities(); m=st[0]
            except: continue
            lr=lig_res(m,cr[0],cr[1])
            if lr is None: continue
            lp=np.array([[a.pos.x,a.pos.y,a.pos.z] for a in lr if a.element.name!="H"])
            pc=pocket_chain(m,lp)
            if pc is None: continue
            models[mdl]=m; ligs[mdl]=lr; pock[mdl]=pc
        if len(models)<2: skipped.append(tgt); continue
        ref=min(models); refpoly=pock[ref]
        dd=DATA/tgt; dd.mkdir(exist_ok=True)
        ref_lig_pos={}  # transformed ligand coords per model (for pocket selection)
        for mdl in models:
            tr=None
            if mdl!=ref:
                try:
                    sup=gemmi.calculate_superposition(refpoly,pock[mdl],gemmi.PolymerType.PeptideL,gemmi.SupSelect.CaP)
                    if not np.isfinite(sup.rmsd): continue
                    tr=sup.transform
                except: continue
            write_lig(ligs[mdl],tr,dd/f"pose-{mdl}.pdb")
            lp=np.array([[(tr.apply(a.pos) if tr else a.pos).x,(tr.apply(a.pos) if tr else a.pos).y,(tr.apply(a.pos) if tr else a.pos).z] for a in ligs[mdl] if a.element.name!="H"])
            ref_lig_pos[mdl]=lp
            write_prot(pock[mdl],tr,dd/f"pocket-{mdl}.pdb",near=lp)        # this pose's OWN AF3 pocket residues
        # cartoon backdrop = ref AF3 protein; union pocket = ref residues near any pose
        write_prot(refpoly,None,dd/"protein.pdb")
        allpos=np.vstack(list(ref_lig_pos.values()))
        write_prot(refpoly,None,dd/"pocket-union.pdb",near=allpos)
        fixed+=1
    print(f"regenerated {fixed} items with AF3 proteins; skipped {len(skipped)}: {skipped}")

if __name__=="__main__": main()
