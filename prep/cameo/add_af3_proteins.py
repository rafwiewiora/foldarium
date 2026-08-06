"""Add the AF3 protein conformation per pose, CRYSTAL-ALIGNED (same seq-Ca transform as the ligand), so
the viewer can TOGGLE between the fixed crystal protein and each pose's own AF3 protein (to check whether
a clash is real or a crystal-rotamer artifact). Writes afprotein-<n>.pdb + afpocket-<n>.pdb per pose, and
an afprotein_ref / afpocket_union for AF3-mode show-all. Heavy atoms only."""
import json, glob, sys
from pathlib import Path
import numpy as np, gemmi, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0,"."); sys.path.insert(0,"../viewer")
import process_cameo as P, align_to_crystal as A
DATA=Path("data"); POCKET_R=5.0
def wpoly(poly,R,t,dest,near=None):
    out=[]; i=0
    for r in poly:
        atoms=[(a, R@np.array([a.pos.x,a.pos.y,a.pos.z])+t) for a in r if a.element.name!="H"]
        if near is not None:
            xyz=np.array([p for _,p in atoms])
            if not len(xyz) or np.min(np.linalg.norm(xyz[:,None]-near[None],axis=2))>=POCKET_R: continue
        for a,p in atoms:
            i+=1
            out.append(f"ATOM  {i:>5d} {a.name[:4]:<4s} {r.name[:3]:>3s} A{r.seqid.num:>4d}    {p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {a.element.name:>2s}")
    out.append("END"); dest.write_text("\n".join(out)+"\n")
d=json.load(open("quiz_items.json")); done=0
for it in d["items"]:
    tgt=it["id"]; week=it["week"]; het=it["ligand"]; amap=P.af3_ligand_map(week,tgt,het); dd=DATA/tgt
    cst=gemmi.read_structure(glob.glob(f"_xtal_cache/*{tgt}*")[0]); cst.setup_entities(); cm=cst[0]
    cligs=[r for ch in cm for r in ch if r.name.upper()==het.upper()]
    cpoly=A.pocket_chain(cm,np.array([[a.pos.x,a.pos.y,a.pos.z] for a in cligs[0] if a.element.name!="H"]))
    union=[]; refmdl=min(c["af3_sample"] for c in it["choices"])
    for c in it["choices"]:
        mdl=c["af3_sample"]; cr=amap.get(mdl)
        try:
            st=gemmi.read_structure(str(P.server_dir(week,tgt)/f"model-{mdl}"/f"model-{mdl}.cif")); st.setup_entities(); m=st[0]
        except: continue
        lr=None
        for ch in m:
            if ch.name==cr[0]:
                for r in ch:
                    if r.name==cr[1]: lr=r
        if lr is None: continue
        apoly=A.pocket_chain(m,np.array([[a.pos.x,a.pos.y,a.pos.z] for a in lr if a.element.name!="H"]))
        sup=A.seq_super(apoly,cpoly)
        if sup is None: continue
        R,t=sup
        lpos=np.array([R@np.array([a.pos.x,a.pos.y,a.pos.z])+t for a in lr if a.element.name!="H"]); union.append(lpos)
        wpoly(apoly,R,t,dd/f"afprotein-{mdl}.pdb")
        wpoly(apoly,R,t,dd/f"afpocket-{mdl}.pdb",near=lpos)
        c["afprotein_file"]=f"data/{tgt}/afprotein-{mdl}.pdb"; c["afpocket_file"]=f"data/{tgt}/afpocket-{mdl}.pdb"
        if mdl==refmdl:
            it["afprotein_ref"]=f"data/{tgt}/afprotein-{mdl}.pdb"; _refpoly,_refRt=apoly,(R,t)
    if union and "afprotein_ref" in it:
        wpoly(_refpoly,_refRt[0],_refRt[1],dd/"afpocket-union.pdb",near=np.vstack(union))
        it["afpocket_union"]=f"data/{tgt}/afpocket-union.pdb"; done+=1
json.dump(d,open("quiz_items.json","w"),indent=2)
print(f"added crystal-aligned AF3 proteins for {done} items")
