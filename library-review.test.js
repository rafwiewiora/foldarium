import test from 'node:test';
import assert from 'node:assert/strict';
import {alignComplexPdb} from './library-alignment.js';
import {LIBRARY_SCHEMA,validateLibrary,stringifyLibrary,ligandConfidence} from './library-format.js';
import {ligandMol} from './library-chemistry.js';
const line=(i,name,chain,xyz)=>`${chain==='A'?'ATOM  ':'HETATM'}${String(i).padStart(5)} ${name.padStart(4)} ${chain==='A'?'ALA':'LIG'} ${chain}${String(i).padStart(4)}    ${xyz.map(x=>x.toFixed(3).padStart(8)).join('')}  1.00 90.00           C`;
test('protein alignment preserves ligand pose relative to its own receptor',()=>{
  const points=[[1,0,0],[0,2,0],[0,0,3],[2,2,1]],ligand=[4,1,2];
  const pdb=ps=>ps.map((p,i)=>line(i+1,i<4?'CA':'C1',i<4?'A':'B',p)).join('\n');
  const reference=pdb([...points,ligand]), moved=pdb([...points,ligand].map(([x,y,z])=>[-y+10,x-5,z+7]));
  const fit=alignComplexPdb(moved,reference);assert.ok(fit.rmsd<1e-6);assert.equal(fit.atoms,4);
  assert.equal(fit.pdb,reference);
  const shifted=pdb([...points,[8,1,2]]), result=alignComplexPdb(shifted,reference);
  assert.equal(result.pdb,shifted,'ligand displacement is not fitted away');
  assert.throws(()=>alignComplexPdb('',''),/three matching/);
});
test('portable library retains exact pose decisions and numeric confidence arrays',()=>{
  const pdb=line(1,'CA','A',[0,0,0])+'\n'+line(2,'C1','B',[1,0,0]);
  const b={schema:LIBRARY_SCHEMA,run:{id:'run',createdAt:'2026-11-12T00:00:00Z',compounds:[{id:'a',smiles:'C'}]},
    poses:[{id:'p',compoundId:'a',status:'complete',result:{pdb,plddt:new Float32Array([80,90]),confidenceLayout:{pdbAtoms:[{chain:'B',slot:1}]}}}],
    decisions:[{id:'d',compoundId:'a',poseId:'p',value:'preferred'}]};
  assert.equal(ligandConfidence(b.poses[0].result),90);assert.deepEqual(JSON.parse(stringifyLibrary(b)).poses[0].result.plddt,[80,90]);
  assert.throws(()=>validateLibrary({...b,decisions:[{compoundId:'a',poseId:'unknown',value:'preferred'}]}),/exact pose/);
  assert.throws(()=>validateLibrary({...b,poses:[...b.poses,...b.poses]}),/Invalid pose/);
});
test('ligand display retains explicit aromatic bonds and formal charge at arbitrary distances',()=>{
  const pdb=[line(1,'C1','B',[0,0,0]),line(2,'C2','B',[50,0,0])].join('\n');
  const sdf=ligandMol({component:{atoms:[{name:'C1',element:6},{name:'C2',element:6,charge:-1}],bonds:[{from:0,to:1,order:4}]}},pdb);
  assert.match(sdf,/  1  2  4/);assert.match(sdf,/M  CHG  1   2  -1/);assert.match(sdf,/50\.0000/);
});
