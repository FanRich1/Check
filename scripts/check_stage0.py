"""Check stage-0 artifacts, not business accuracy or application readiness.

Standard library only. --portable skips private real-source checks.
"""
import argparse
from collections import Counter
from decimal import Decimal
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
checks = []


def check(condition, name):
    checks.append({'check': name, 'passed': bool(condition)})


def read(path):
    return json.loads(path.read_text(encoding='utf-8'),
                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--portable', action='store_true')
    args = parser.parse_args()
    base = ROOT/'tests/fixtures'
    catalog = read(base/'scenario-catalog.json')['cases']
    ids = [c['case_id'] for c in catalog]
    check(len(ids) == len(set(ids)) == 60, '60 unique scenario slots')
    check(Counter(c['split'] for c in catalog) == {'development':30,'validation':15,'holdout':15}, '30/15/15 planned split')
    families = {}
    for c in catalog:
        families.setdefault(c['family_id'], set()).add(c['split'])
    check(all(len(s) == 1 for s in families.values()), 'no declared family crosses splits; later lineage review still required')
    requirements = {r for c in catalog for r in c['requirements']}
    all_requirements = {f'R{i:02d}' for i in range(1,34)}
    check(requirements == all_requirements, 'R01-R33 scenario planning coverage')
    matrix = (ROOT/'docs/requirements-matrix.md').read_text(encoding='utf-8')
    check(set(re.findall(r'\| (R\d{2}) \|', matrix)) == all_requirements, 'R01-R33 requirements matrix coverage')
    materialized = [c for c in catalog if c['status']=='materialized_not_executed']
    check(len(materialized) == 12, '12 materialized authored fixtures')
    check(all(c['split']=='development' for c in materialized), 'no holdout documents authored for development')
    core = {'operation','document_info','transaction_party','charge_details','financial_attributes','audit_trail'}
    for c in materialized:
        cid=c['case_id']; folder=base/'synthetic'/cid
        check(all((base/f).is_file() for f in c['files']), cid+' files exist')
        req=read(folder/'request.json'); expected=read(folder/'expected.json')
        raw=(folder/'input.eml').read_bytes()
        check(hashlib.sha256(raw).hexdigest()==expected['input_sha256'], cid+' frozen input hash')
        mail=BytesParser(policy=policy.default).parsebytes(raw)
        check(not mail.defects and 'SYNTHETIC' in str(mail['Subject']), cid+' valid synthetic EML')
        src={d['source_id'] for d in req['documents']} | {f"existing:{r['record_id']}" for r in req['existing_bills']}
        output=expected['expected_records']
        for i,r in enumerate(output):
            tag=f'{cid} row {i+1}'
            check(core <= r.keys(), tag+' core contract groups')
            amount=r['charge_details']['amount']
            check(amount is None or type(amount) in (int,float),tag+' amount is number/null')
            check(r['operation'] in {'CREATE','UPDATE','NO_CHANGE','PENDING_REVIEW'},tag+' operation enum')
            check(all(s in src for s in r['audit_trail']['sources']),tag+' source references')
            evidence=[e for v in r['audit_trail']['field_evidence'].values() for e in v]
            check(all(e['source_id'] in src and e['quote'] and e['locator'] for e in evidence),tag+' evidence references')
            if r['operation'] in {'CREATE','UPDATE'}:
                check(amount is not None and r['charge_details']['currency'] and
                      r['financial_attributes']['accounting_entity'] and
                      r['financial_attributes']['posting_type'] in {'AR','AP'},tag+' accepted core fields present')
            if r['operation']=='PENDING_REVIEW':
                check(any(x['blocks_automation'] for x in r['audit_trail']['issues']),tag+' explicit blocking reason')
        before=req['existing_bills']; after=expected['expected_existing_snapshot_after']
        if expected['authorized_change_paths']:
            check(len(before)==len(after)==1,cid+' update snapshot identity')
            clone=json.loads(json.dumps(before)); clone[0]['charge_details']['amount']=55
            check(after==clone,cid+' only authorized amount changed, including extension preservation')
        else:
            check(before==after,cid+' no unauthorized snapshot mutation')
    def result(cid):
        return read(base/'synthetic'/cid/'expected.json')
    check([r['charge_details']['amount'] for r in result('DEV-005')['expected_records']]==[65,35], 'same-name fixture retains both actual lines')
    check(result('DEV-010')['expected_records']==[], 'conditional tariff has no synthetic fee')
    check(result('DEV-007')['expected_records'][0]['charge_details']['currency'] is None, 'ambiguous dollar symbol is not USD')
    check(result('DEV-009')['expected_records'][0]['charge_details']['payment_term'] is None, 'COD is not CC')
    if not args.portable:
        local=ROOT/'data/local/stage0'
        manifest=read(local/'source-manifest.json'); sources=manifest['sources']
        check(len(sources)==7 and sum(s['parent_id'] is None for s in sources)==4, '4 top-level and 3 embedded sources')
        for s in sources:
            f=ROOT/s['path']
            check(f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest()==s['sha256'],s['source_id']+' actual source hash')
        check(sum(s.get('pages',0) for s in sources)==6,'6 PDF pages inventoried')
        golden=read(local/'golden-real.json')
        check(len(golden['cases'])==4,'4 visually reviewed real case groups')
        fees=[f for c in golden['cases'] for f in c['fees']]
        check(len(fees)==7,'7 real fee candidates labelled')
        check(sum(f['charge_details']['currency'] is not None for f in fees)==6,'6 explicit currency candidates, 1 ambiguous')
        for c in golden['cases']:
            check(all(f['expected_operation_without_context']=='PENDING_REVIEW' for f in c['fees']),c['case_id']+' no invented accounting context')
            if c['document_facts'].get('total') is not None:
                total=sum(Decimal(str(f['charge_details']['amount'])) for f in c['fees'])
                check(total==Decimal(str(c['document_facts']['total'])),c['case_id']+' visually labelled fees reconcile')
        check(all(x['expected_new_fee_count']==0 for x in golden['attachment_classification']),'mail attachments do not invent new monetary lines')
        check(golden['independent_human_signoff'] is False,'review provenance is honestly recorded')
    docs=[ROOT/'README.md',*(ROOT/'docs').glob('*.md')]
    for f in docs:
        for target in re.findall(r'\]\(([^)]+)\)',f.read_text(encoding='utf-8')):
            if '://' not in target and not target.startswith('#'):
                check((f.parent/target).exists(),f'{f.name} link: {target}')
    failed=[c for c in checks if not c['passed']]
    report={'scope':'stage0 artifact integrity, not application evaluation',
            'mode':'portable' if args.portable else 'local_with_real_sources',
            'total_checks':len(checks),'passed':len(checks)-len(failed),'failed':len(failed),
            'business_tests_executed':0,'scenario_slots':60,'materialized_fixtures':12,
            'planned_not_materialized':48,'checks':checks}
    out=ROOT/'outputs/stage0'; out.mkdir(parents=True,exist_ok=True)
    name='artifact-check-portable.json' if args.portable else 'artifact-check.json'
    (out/name).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f"Stage-0 artifact checks: {report['passed']}/{len(checks)} passed; {len(failed)} failed. Business tests executed: 0.")
    for x in failed: print('FAIL:',x['check'])
    raise SystemExit(bool(failed))


if __name__=='__main__':
    main()
