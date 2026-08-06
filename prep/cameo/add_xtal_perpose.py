"""Add (a) per-pose full AF3 protein (afprotein-<n>.pdb) so the cartoon backbone matches the per-pose
sticks, and (b) the crystal reference (xtal_lig/xtal_protein) aligned into the AF3 model-1 frame, to
reveal AFTER answering. All heavy-atom only (no fabricated H). Updates quiz_items.json with file paths."""
import json, warnings, sys, glob, difflib
from pathlib import Path
import numpy as np, gemmi
warnings.filterwarnings("ignore")
sys.path.insert(0,"."); sys.path.insert(0,"../viewer")
import process_cameo as P
HERE=Path(".").resolve(); DATA=HERE/"data"

def ca_list(poly):
    out=[]
    for r in poly:
        a=r.find_atom("CA","*"); out.append(None if a is None else np.array([a.pos.x,a.pos.y,a.pos.z]))
    return out
def one_letter(poly):
    return gemmi.one_letter_code([r.name for r in poly])
def kabsch(Pm,Qm):
    Pc,Qc=Pm.mean(0),Qm.mean(0); H=(Pm-Pc).T@(Qm-Qc); U,S,Vt=np.linalg.svd(H)
    d=np.sign(np.linalg.det(Vt.T@U.T)); R=Vt.T@np.diag([1,1,d])@U.T; return R,Qc-R@Pc
def seq_super(mob_poly, ref_poly):
    rs,ms=one_letter(ref_poly),one_letter(mob_poly); rca,mca=ca_list(ref_poly),ca_list(mob_poly)
    Pm,Qm=[],[]
    for a,b,size in difflib.SequenceMatcher(None,rs,ms,autojunk=False).get_matching_blocks():
        for k in range(size):
            if rca[a+k] is not None and mca[b+k] is not None: Qm.append(rca[a+k]); Pm.append(mca[b+k])
    if len(Pm)<6: return None
    return kabsch(np.array(Pm),np.array(Qm))   # maps mob -> ref
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
def write_poly(poly, R, t, dest):
    out=[]; i=0
    for r in poly:
        for a in r:
            if a.element.name=="H": continue
            i+=1; p=(R@np.array([a.pos.x,a.pos.y,a.pos.z])+t) if R is not None else np.array([a.pos.x,a.pos.y,a.pos.z])
            out.append(f"ATOM  {i:>5d} {a.name[:4]:<4s} {r.name[:3]:>3s} A{r.seqid.num:>4d}    {p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {a.element.name:>2s}")
    out.append("END"); dest.write_text("\n".join(out)+"\n")
def write_lig(res, R, t, dest):
    out=[]; i=0
    for a in res:
        if a.element.name=="H": continue
        i+=1; p=(R@np.array([a.pos.x,a.pos.y,a.pos.z])+t) if R is not None else np.array([a.pos.x,a.pos.y,a.pos.z])
        out.append(f"HETATM{i:>5d} {a.name[:4]:<4s} LIG X   1    {p[0]:8.3f}{p[1]:8.3f}{p[2]:8.3f}  1.00  0.00          {a.element.name:>2s}")
    out.append("END"); dest.write_text("\n".join(out)+"\n")

def main():
    d=json.load(open("quiz_items.json")); ok=0; noxtal=[]
    for it in d["items"]:
        tgt=it["id"]; week=it["week"]; het=it["ligand"]; amap=P.af3_ligand_map(week,tgt,het)
        polys={}; ligs={}
        for c in it["choices"]:
            mdl=c["af3_sample"]; cr=amap.get(mdl)
            if not cr: continue
            try:
                st=gemmi.read_structure(str(P.server_dir(week,tgt)/f"model-{mdl}"/f"model-{mdl}.cif")); st.setup_entities(); m=st[0]
            except: continue
            lr=None
            for ch in m:
                if ch.name==cr[0]:
                    for r in ch:
                        if r.name==cr[1]: lr=r
            if lr is None: continue
            lp=np.array([[a.pos.x,a.pos.y,a.pos.z] for a in lr if a.element.name!="H"])
            polys[mdl]=pocket_chain(m,lp); ligs[mdl]=lr
        if len(polys)<2: continue
        ref=min(polys); refpoly=polys[ref]; dd=DATA/tgt
        # per-pose full protein cartoon (ref frame)
        for c in it["choices"]:
            mdl=c["af3_sample"]
            if mdl not in polys: c["afprotein_file"]=f"data/{tgt}/protein.pdb"; continue
            if mdl==ref: Rt=(None,None)
            else:
                sup=seq_super(polys[mdl],refpoly)
                Rt=sup if sup else (None,None)
            write_poly(polys[mdl],Rt[0],Rt[1],dd/f"afprotein-{mdl}.pdb")
            c["afprotein_file"]=f"data/{tgt}/afprotein-{mdl}.pdb"
        # crystal reference -> ref frame
        f=glob.glob(f"_xtal_cache/*{tgt}*")
        if f:
            cst=gemmi.read_structure(f[0]); cst.setup_entities(); cm=cst[0]
            cligs=[r for ch in cm for r in ch if r.name.upper()==het.upper()]
            if cligs:
                clp=np.array([[a.pos.x,a.pos.y,a.pos.z] for a in cligs[0] if a.element.name!="H"])
                cpoly=pocket_chain(cm,clp); sup=seq_super(cpoly,refpoly) if cpoly else None
                if sup:
                    R,t=sup
                    # pick crystal ligand copy whose transformed centroid is nearest the ref AF3 ligand
                    refl=np.array([[float(l[30:38]),float(l[38:46]),float(l[46:54])] for l in open(dd/f"pose-{ref}.pdb") if l[:6]=="HETATM"])
                    best=min(cligs,key=lambda r:np.linalg.norm((R@np.array([[a.pos.x,a.pos.y,a.pos.z] for a in r if a.element.name!="H"]).mean(0)+t)-refl.mean(0)))
                    write_lig(best,R,t,dd/"xtal_lig.pdb"); write_poly(cpoly,R,t,dd/"xtal_protein.pdb")
                    it["xtal_lig_file"]=f"data/{tgt}/xtal_lig.pdb"; it["xtal_protein_file"]=f"data/{tgt}/xtal_protein.pdb"
                    ok+=1
                else: noxtal.append(tgt)
            else: noxtal.append(tgt)
        else: noxtal.append(tgt)
    json.dump(d,open("quiz_items.json","w"),indent=2)
    print(f"per-pose proteins added; crystal ref added for {ok}/{len(d['items'])} items; no-xtal: {noxtal}")
if __name__=="__main__": main()
