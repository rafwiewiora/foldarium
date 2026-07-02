"""Align EVERYTHING to the crystal (ground truth), exactly like the viewer. The crystal protein is the
FIXED frame: each AF3 model's pocket chain is superposed onto the crystal pocket chain (sequence-matched
Ca) and only the AF3 LIGAND is kept, transformed into the crystal frame. Display = fixed crystal protein
cartoon + fixed crystal pocket sticks + moving AF3 ligand poses; crystal ligand hidden until reveal.
Heavy atoms only (AF3 has no H). One pocket file + one protein file per item (fixed) so the backbone
never moves as you scroll poses."""
import json, warnings, sys, glob, difflib
from pathlib import Path
import numpy as np, gemmi
warnings.filterwarnings("ignore")
sys.path.insert(0,"."); sys.path.insert(0,"../viewer")
import process_cameo as P
HERE=Path("."); DATA=HERE/"data"; POCKET_R=5.0

def ca_list(poly):
    out=[]
    for r in poly:
        a=r.find_atom("CA","*"); out.append(None if a is None else np.array([a.pos.x,a.pos.y,a.pos.z]))
    return out
def one_letter(poly): return gemmi.one_letter_code([r.name for r in poly])
def kabsch(Pm,Qm):
    Pc,Qc=Pm.mean(0),Qm.mean(0); H=(Pm-Pc).T@(Qm-Qc); U,S,Vt=np.linalg.svd(H)
    d=np.sign(np.linalg.det(Vt.T@U.T)); R=Vt.T@np.diag([1,1,d])@U.T; return R,Qc-R@Pc
def seq_super(mob_poly, ref_poly):
    rs,ms=one_letter(ref_poly),one_letter(mob_poly); rca,mca=ca_list(ref_poly),ca_list(mob_poly); Pm,Qm=[],[]
    for a,b,size in difflib.SequenceMatcher(None,rs,ms,autojunk=False).get_matching_blocks():
        for k in range(size):
            if rca[a+k] is not None and mca[b+k] is not None: Qm.append(rca[a+k]); Pm.append(mca[b+k])
    return kabsch(np.array(Pm),np.array(Qm)) if len(Pm)>=6 else None
def pocket_chain(m, ligpos):
    best=None; bestn=-1
    for ch in m:
        poly=ch.get_polymer()
        if len(poly)<5: continue
        ca=np.array([[a.pos.x,a.pos.y,a.pos.z] for r in poly for a in r if a.name=="CA"])
        if not len(ca): continue
        n=int((np.min(np.linalg.norm(ca[:,None]-ligpos[None],axis=2),axis=1)<10).sum())
        if n>bestn: bestn=n; best=poly
    return best
def write_poly(poly, dest, near=None):
    out=[]; i=0
    for r in poly:
        atoms=[(a,np.array([a.pos.x,a.pos.y,a.pos.z])) for a in r if a.element.name!="H"]
        if near is not None:
            xyz=np.array([p for _,p in atoms])
            if not len(xyz) or np.min(np.linalg.norm(xyz[:,None]-near[None],axis=2))>=POCKET_R: continue
        for a,p in atoms:
            i+=1
            out.append(f"ATOM  {i:>5d} {a.name[:4]:<4s} {r.name[:3]:>3s} A{r.seqid.num:>4d}    {p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {a.element.name:>2s}")
    out.append("END"); dest.write_text("\n".join(out)+"\n")
def write_lig(atoms, dest):   # atoms = list of (element,name,xyz)
    out=[f"HETATM{i+1:>5d} {nm[:4]:<4s} LIG X   1    {p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {el:>2s}" for i,(el,nm,p) in enumerate(atoms)]
    out.append("END"); dest.write_text("\n".join(out)+"\n")

def main():
    d=json.load(open("quiz_items.json")); ok=0; drop=[]
    for it in list(d["items"]):
        tgt=it["id"]; week=it["week"]; het=it["ligand"]; amap=P.af3_ligand_map(week,tgt,het); dd=DATA/tgt
        f=glob.glob(f"_xtal_cache/*{tgt}*")
        if not f: drop.append((tgt,"no crystal")); d["items"].remove(it); continue
        cst=gemmi.read_structure(f[0]); cst.setup_entities(); cm=cst[0]
        cligs=[r for ch in cm for r in ch if r.name.upper()==het.upper()]
        if not cligs: drop.append((tgt,"no xtal lig")); d["items"].remove(it); continue
        clp=np.array([[a.pos.x,a.pos.y,a.pos.z] for a in cligs[0] if a.element.name!="H"])
        cpoly=pocket_chain(cm,clp)                          # CRYSTAL pocket chain = the fixed frame
        if cpoly is None: drop.append((tgt,"no xtal pocket")); d["items"].remove(it); continue
        # align each AF3 model's ligand onto the crystal frame
        posepts=[]
        for c in it["choices"]:
            mdl=c["af3_sample"]; cr=amap.get(mdl)
            if not cr: c["_bad"]=True; continue
            try:
                st=gemmi.read_structure(str(P.server_dir(week,tgt)/f"model-{mdl}"/f"model-{mdl}.cif")); st.setup_entities(); m=st[0]
            except: c["_bad"]=True; continue
            lr=None
            for ch in m:
                if ch.name==cr[0]:
                    for r in ch:
                        if r.name==cr[1]: lr=r
            apoly=pocket_chain(m, np.array([[a.pos.x,a.pos.y,a.pos.z] for a in lr if a.element.name!="H"])) if lr else None
            sup=seq_super(apoly,cpoly) if (lr and apoly) else None
            if lr is None or sup is None: c["_bad"]=True; continue
            R,t=sup
            atoms=[(a.element.name,a.name,(R@np.array([a.pos.x,a.pos.y,a.pos.z])+t)) for a in lr if a.element.name!="H"]
            write_lig(atoms, dd/f"pose-{mdl}.pdb")
            posepts.append(np.array([p for _,_,p in atoms]))
        if sum(1 for c in it["choices"] if not c.get("_bad"))<3:
            drop.append((tgt,"too few aligned")); d["items"].remove(it); continue
        allpose=np.vstack(posepts)
        # FIXED crystal protein cartoon + crystal pocket sticks (near the poses); crystal ligand for reveal
        write_poly(cpoly, dd/"protein.pdb")
        write_poly(cpoly, dd/"pocket.pdb", near=allpose)
        # crystal ligand copy nearest the poses
        best=min(cligs,key=lambda r:np.linalg.norm(np.array([[a.pos.x,a.pos.y,a.pos.z] for a in r if a.element.name!="H"]).mean(0)-allpose.mean(0)))
        write_lig([(a.element.name,a.name,np.array([a.pos.x,a.pos.y,a.pos.z])) for a in best if a.element.name!="H"], dd/"xtal_lig.pdb")
        # update schema: fixed protein + pocket, per-choice pose only
        it["protein_file"]=f"data/{tgt}/protein.pdb"; it["pocket_file"]=f"data/{tgt}/pocket.pdb"; it["xtal_lig_file"]=f"data/{tgt}/xtal_lig.pdb"
        it.pop("pocket_union_file",None); it.pop("xtal_protein_file",None)
        for c in it["choices"]:
            c.pop("pocket_file",None); c.pop("afprotein_file",None); c.pop("_bad",None)
        ok+=1
    json.dump(d,open("quiz_items.json","w"),indent=2)
    print(f"crystal-frame aligned {ok} items; dropped {len(drop)}: {drop}")
if __name__=="__main__": main()
