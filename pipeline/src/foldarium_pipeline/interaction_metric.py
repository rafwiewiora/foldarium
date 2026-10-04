"""Blind-safe availability contract for optional ProLIF H-bond evidence."""
from collections.abc import Mapping

HBOND_METRIC='prolif_hbond_residue_count'
HBOND_POLICY='prolif-implicit-hbond-unique-protein-residue/v2'
AVAILABILITY_POLICY='foldarium.prolif-availability/v1'
UNAVAILABLE_FIELDS={
    'status':'unavailable','availability_policy':AVAILABILITY_POLICY,
    'reason':'unsupported_receptor_residue','unsupported_residues':['UNK'],
}

def normalize_interaction_count(raw):
    if not isinstance(raw,Mapping) or raw.get('metric')!=HBOND_METRIC:
        raise ValueError('interaction_count is invalid')
    value=raw.get('value');policy=raw.get('policy')
    if not isinstance(policy,str) or not policy.strip():raise ValueError('interaction_count policy is invalid')
    if value is None:
        if set(raw)!={'metric','value','policy',*UNAVAILABLE_FIELDS} or raw.get('policy')!=HBOND_POLICY or any(raw.get(k)!=v for k,v in UNAVAILABLE_FIELDS.items()):
            raise ValueError('interaction_count unavailable disposition is invalid')
        return {'metric':HBOND_METRIC,'value':None,'policy':HBOND_POLICY,**{**UNAVAILABLE_FIELDS,'unsupported_residues':['UNK']}}
    if isinstance(value,bool) or not isinstance(value,int) or value<0 or any(k in raw for k in UNAVAILABLE_FIELDS):
        raise ValueError('interaction_count is invalid')
    return {'metric':HBOND_METRIC,'value':value,'policy':policy.strip()}
