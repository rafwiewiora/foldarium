"""Can per-pose PHYSICS select the correct pose where AF3's confidence can't — on the AMBIGUOUS set?
Per-pose features (clash, min-dist, contacts, burial, consensus self-distance, ligand pLDDT) computed
from each AF3 model (ligand vs its OWN protein); target ligand via CAMEO's chain map (NOT largest =
cholesterol). Grouped-by-protein CV classifier + leakage-free rules. Selection compared to AF3 pose-1,
random (k/n), and oracle, stratified by whether the ensemble has a clearly-wrong pose (max>=3A)."""
import glob, re, sys
from pathlib import Path
from collections import defaultdict
from statistics import median, mean
import numpy as np, gemmi, warnings
warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent/"viewer"))
import process_cameo as P
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import cross_val_predict, GroupKFold
from sklearn.metrics import roc_auc_score

VDW={"C":1.7,"N":1.55,"O":1.52,"F":1.47,"P":1.8,"S":1.8,"CL":1.75,"BR":1.85,"I":1.98,"H":1.2}
vdw=lambda e:VDW.get(e.upper(),1.7)

def feats_for(date, tgt, het, amap, pm):
    rows=[]; ligcoords={}; polys_by={}
    for mdl in pm:
        cif=P.server_dir(date,tgt)/f"model-{mdl}"/f"model-{mdl}.cif"
        try: st=gemmi.read_structure(str(cif)); st.setup_entities(); m=st[0]
        except: continue
        cr=amap.get(mdl)
        if not cr: continue
        lig=None
        for ch in m:
            if ch.name!=cr[0]: continue
            for r in ch:
                if r.name==cr[1]: lig=r
        if lig is None: continue
        lp=np.array([[a.pos.x,a.pos.y,a.pos.z] for a in lig if a.element.name!="H"])
        lr=np.array([vdw(a.element.name) for a in lig if a.element.name!="H"])
        pp=[];pr=[]
        polys=[ch.get_polymer() for ch in m if len(ch.get_polymer())>5]
        for ch in m:
            for res in ch.get_polymer():
                for a in res:
                    if a.element.name=="H": continue
                    pp.append([a.pos.x,a.pos.y,a.pos.z]); pr.append(vdw(a.element.name))
        if not pp or not len(lp): continue
        pp=np.array(pp); pr=np.array(pr)
        D=np.sqrt(((lp[:,None]-pp[None])**2).sum(-1)); ratio=D/(lr[:,None]+pr[None])
        rows.append(dict(mdl=mdl, rmsd=pm[mdl], correct=int(pm[mdl]<2),
            plddt=float(np.mean([a.b_iso for a in lig if a.element.name!="H"])),
            clash=int((ratio<0.75).sum()), mindist=float(D.min()),
            contacts=int((D.min(0)<4.5).sum()), buried=float((D.min(1)<4.5).mean())))
        ligcoords[mdl]=lp; polys_by[mdl]=max(polys,key=len)
    # consensus self-distance (superpose proteins onto first)
    if len(rows)>=3 and ligcoords:
        ref=min(ligcoords); refp=polys_by[ref]; co={}
        for mdl in ligcoords:
            try:
                sup=gemmi.calculate_superposition(refp,polys_by[mdl],gemmi.PolymerType.PeptideL,gemmi.SupSelect.CaP)
                if not np.isfinite(sup.rmsd): continue
                co[mdl]=np.array([[p.x,p.y,p.z] for p in (sup.transform.apply(gemmi.Position(*x)) for x in ligcoords[mdl])])
            except: pass
        for r in rows:
            o=[co[k] for k in co if k!=r["mdl"] and r["mdl"] in co and co[k].shape==co.get(r["mdl"],np.empty((0,3))).shape]
            r["consensus"]=float(np.mean([np.sqrt(((co[r["mdl"]]-c)**2).sum(1).mean()) for c in o])) if (r["mdl"] in co and o) else np.nan
    return rows

def main():
    bases=sorted(glob.glob(str(P.CAMEO/"*"/"*"/"servers"/"server993")))
    allrows=[]; ligand_meta={}; n=0
    for sd in bases:
        mm=re.search(r"modeling/([\d.]+)/([0-9A-Za-z]+)/",sd); date,tgt=mm.group(1),mm.group(2)
        try:
            ligs,_=P.collect_target(date,tgt); het=P.select_target_het(ligs)
            if not het: continue
            pm=P.per_model_rmsd(ligs[het])
            if len(pm)<3 or min(pm.values())>=2: continue
            amap=P.af3_ligand_map(date,tgt,het)
            rows=feats_for(date,tgt,het,amap,pm)
            rows=[r for r in rows if "consensus" in r and not np.isnan(r.get("consensus",np.nan))]
            if len(rows)<3: continue
            key=(tgt,het)
            seqcif=P.server_dir(date,tgt)/"model-1"/"model-1.cif"
            try:
                st=gemmi.read_structure(str(seqcif)); st.setup_entities()
                seq=max((ch.get_polymer() for ch in st[0] if len(ch.get_polymer())>5),key=len).make_one_letter_sequence()
            except: seq=tgt
            for r in rows: r["target"]=tgt; r["seq"]=seq; allrows.append(r)
            ligand_meta[key]=dict(maxr=max(pm.values()))
            n+=1
            if n%50==0: print("...",n,flush=True)
        except Exception: continue
    print(f"\nligands: {n}, poses: {len(allrows)}")
    y=np.array([r["correct"] for r in allrows]); groups=np.array([r["seq"] for r in allrows])
    X=lambda cols:np.array([[float(r[c]) for c in cols] for r in allrows])
    gkf=GroupKFold(5)
    def auc(cols,lab):
        p=cross_val_predict(make_pipeline(StandardScaler(),LogisticRegression(max_iter=3000)),X(cols),y,cv=gkf,groups=groups,method="predict_proba")[:,1]
        print(f"  {lab:30s}: AUROC {roc_auc_score(y,p):.3f}"); return p
    print("PER-POSE classification (GroupKFold-by-protein):")
    auc(["plddt"],"confidence (pLDDT)"); auc(["clash","mindist","contacts","buried"],"physics")
    auc(["consensus"],"consensus self-dist")
    proba=auc(["plddt","clash","mindist","contacts","buried","consensus"],"all features")
    # selection
    bylig=defaultdict(list)
    for r,pr in zip(allrows,proba): bylig[(r["target"])].append((r,pr))
    def sel(items,label):
        if not items: print(f"  {label}: n=0"); return
        acc=defaultdict(float); k=len(items)
        for v in items:
            rs=[r for r,_ in v]
            acc["oracle"]+=any(r["correct"] for r in rs)
            acc["AF3 pose-1"]+=([r for r in rs if r["mdl"]==1] or [rs[0]])[0]["correct"]
            acc["random k/n"]+=sum(r["correct"] for r in rs)/len(rs)
            acc["clf:all-features"]+=max(v,key=lambda x:x[1])[0]["correct"]
            acc["rank:pLDDT"]+=max(rs,key=lambda r:r["plddt"])["correct"]
            acc["rank:consensus"]+=min(rs,key=lambda r:r["consensus"])["correct"]
            acc["rule:max-contacts"]+=max(rs,key=lambda r:r["contacts"])["correct"]
            acc["rule:buried-clash"]+=max(rs,key=lambda r:r["buried"]-0.1*r["clash"])["correct"]
        print(f"  {label} (n={k}):")
        for kk in ["AF3 pose-1","random k/n","rank:pLDDT","rank:consensus","rule:max-contacts",
                   "rule:buried-clash","clf:all-features","oracle"]:
            print(f"      {kk:22s} {100*acc[kk]/k:.0f}%")
    allitems=list(bylig.values())
    amb=[v for v in allitems if max(r["rmsd"] for r,_ in v)>=3]
    amb5=[v for v in allitems if max(r["rmsd"] for r,_ in v)>=5]
    print("\nSELECTION (% ligands whose chosen pose is <2A):")
    sel(allitems,"ALL oracle-correct")
    sel(amb,"AMBIGUOUS (a pose >=3A present)")
    sel(amb5,"HARD (a pose >=5A present)")

if __name__=="__main__":
    main()
