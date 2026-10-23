"""Build authored synthetic stage-0 fixtures, not application predictions."""
from pathlib import Path
from email.message import EmailMessage
from email import policy
from copy import deepcopy
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'tests/fixtures'
ORIGIN = 'TEST ORIGIN LOGISTICS'
DEST = 'TEST DESTINATION AGENCY'


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def record(cid, amount=40, operation='CREATE', currency='USD', entity=ORIGIN,
           term='PP', raw='Handling Fee', line='L1', existing=None, reasons=()):
    return {
        'record_id': existing or f'{cid}-{line}', 'existing_record_id': existing,
        'operation': operation,
        'document_info': {'document_type': 'Invoice', 'reference_no': f'INV-{cid}', 'document_date': '2026-09-20'},
        'transaction_party': {'issuer': DEST, 'recipient': ORIGIN},
        'charge_details': {'charge_item': 'Handling Charge', 'charge_item_raw': raw,
                           'amount': amount, 'currency': currency, 'payment_term': term,
                           'quantity': None, 'unit_price': None},
        'financial_attributes': {'accounting_entity': entity, 'posting_type': 'AP' if entity else None},
        'audit_trail': {'ai_reasoning': ['依据明确费用及交易方向；测试预期，不是模型运行结果。'],
                        'sources': [f'{cid}-source'], 'field_evidence': {},
                        'issues': [{'code': r, 'severity': 'error', 'blocks_automation': True,
                                    'field': None, 'message': r} for r in reasons], 'changes': []},
        'extensions': {'cost_center': 'SYNTHETIC-ONLY'},
    }


def build_cases():
    cases = []
    for n in range(1, 13):
        cid = f'DEV-{n:03d}'
        body = (f'本邮件为合成测试资料。单据类型 Invoice；单据编号 INV-{cid}；日期 2026-09-20。\n'
                f'账单签发方和收款方：{DEST}。账单接收方和付款方：{ORIGIN}。\n')
        old = []
        expected = [record(cid)]
        authorized = []
        status = 'processed'
        if n == 1:
            body += '费用行 L1：Handling Fee，金额 USD 40，付款方式 PP。请建立此新费用。\n'
        elif n == 2:
            body += '费用行 L1：Handling Fee，金额 USD 40，付款方式 PP。重新发送同一账单，无任何修改。\n'
            old = [record(cid, existing=f'{cid}-old')]
            expected = [record(cid, operation='NO_CHANGE', existing=f'{cid}-old')]
        elif n in (3, 4):
            body += ('现明确请求仅将已有费用行 L1 的金额由 40 改为 55，其他字段包括币种、付款方式均保持原值。\n'
                     '费用名称 Handling Charge。\n')
            if n == 3:
                body += '币种 USD，付款方式 PP。\n'
            old = [record(cid, existing=f'{cid}-old')]
            expected = [record(cid, amount=55, operation='UPDATE', existing=f'{cid}-old')]
            authorized = ['/charge_details/amount']
        elif n == 5:
            body += ('费用行 L1：Handling Charge，金额 USD 65，付款方式 PP。\n'
                     '费用行 L2：Handling Charge，金额 USD 35，付款方式 PP。\n合计 USD 100。两项为不同实际费用。\n')
            expected = [record(cid, amount=65, raw='Handling Charge'), record(cid, amount=35, raw='Handling Charge', line='L2')]
        elif n == 6:
            body += '请把此单的 Handling Charge 改成 USD 55，付款方式 PP；未提供要改哪条费用的行身份。\n'
            old = [record(cid, existing=f'{cid}-old-a'), record(cid, amount=60, existing=f'{cid}-old-b', line='L2')]
            expected = [record(cid, amount=55, operation='PENDING_REVIEW', reasons=['MULTIPLE_MATCHES'])]
        elif n == 7:
            body += '费用行 L1：Handling Fee，金额 $50，付款方式 PP。没有币种全称或代码。\n'
            expected = [record(cid, amount=50, currency=None, operation='PENDING_REVIEW', reasons=['AMBIGUOUS_CURRENCY'])]
        elif n == 8:
            body += '费用行 L1：Handling Fee，金额 USD 40，付款方式 PP。\n'
            expected = [record(cid, entity=None, operation='PENDING_REVIEW', reasons=['MISSING_ACCOUNTING_ENTITY'])]
        elif n == 9:
            body += '费用行 L1：Handling Fee，金额 USD 40。结算条件 COD，没有费用预付/到付说明。\n'
            expected = [record(cid, term=None)]
            expected[0]['charge_details']['payment_term_raw'] = 'COD'
        elif n == 10:
            body += '这只是一般收费说明：若取消转关申请将收 USD 25。本次没有发生取消，也没有任何实际费用。\n'
            expected = []
            status = 'no_charge'
        elif n == 11:
            body += ('费用行 L1：Handling Charge，金额 USD 40，付款方式 PP。\n'
                     '费用行 L2：Handling Charge，金额 USD 60，付款方式 PP。\n单据明确声称上述两行合计 USD 150，无其他费用或税。\n')
            expected = [record(cid, amount=a, raw='Handling Charge', line=f'L{i}', operation='PENDING_REVIEW', reasons=['TOTAL_MISMATCH'])
                        for i, a in enumerate([40, 60], 1)]
        elif n == 12:
            body += '费用行 L1：Handling Fee，金额 USD 55，付款方式 PP。仅转发供参考，并未请求修改旧账单。\n'
            old = [record(cid, existing=f'{cid}-old')]
            expected = [record(cid, amount=55, existing=f'{cid}-old', operation='PENDING_REVIEW', reasons=['UNAUTHORIZED_CHANGE'])]
        if any(r['operation'] == 'PENDING_REVIEW' for r in expected):
            status = 'pending_review'
        for r in expected:
            evidence = {'source_id': f'{cid}-source', 'locator': {'kind': 'email_body', 'section': 'plain'}, 'quote': body}
            r['audit_trail']['field_evidence']['/charge_details/amount'] = [evidence]
            if n in (3, 4):
                inherited = {'source_id': f'existing:{cid}-old', 'locator': {'kind': 'existing_record', 'pointer': '/charge_details/currency'}, 'quote': 'USD'}
                r['audit_trail']['sources'].append(f'existing:{cid}-old')
                r['audit_trail']['field_evidence']['/charge_details/currency'] = [inherited]
                r['audit_trail']['changes'] = [{'path': '/charge_details/amount', 'old': 40, 'new': 55, 'evidence': [evidence]}]
        # Apply expected authorized edits only; pending candidates never mutate snapshots.
        after = deepcopy(old)
        if n in (3, 4):
            after[0]['charge_details']['amount'] = 55
        cases.append((cid, body, old, expected, authorized, status, after))
    return cases


DEV = [
    ('明确新费用', ['R10','R11','R12','R14','R17','R22','R28','R33']),
    ('原样账单重传', ['R09','R23']), ('明确修改金额', ['R19','R20']),
    ('缺失字段继承', ['R20','R21']), ('同单同名两行', ['R12','R18']),
    ('旧费用多候选', ['R19','R24']), ('美元符号币种不明', ['R15','R27']),
    ('记账主体缺失', ['R22','R27']), ('COD 不等于 CC', ['R16']),
    ('条件条款不计费', ['R13','R18']), ('明细总额冲突', ['R25']),
    ('新值不等于修改授权', ['R20','R24']), ('显式清空可空字段', ['R21']),
    ('无效旧账单或重复 ID', ['R09']), ('同业务双方 AR 视角', ['R22']),
    ('Credit Note 冲销方向', ['R22','R25']), ('含税折扣费用', ['R13','R25']),
    ('多币种不得直接相加', ['R15','R25']), ('OCR 标识符混淆', ['R26']),
    ('中文 HTML 邮件编码', ['R02']), ('嵌套邮件和错误 MIME', ['R03']),
    ('扫描多页 PDF', ['R04','R06']), ('PNG/JPG/TIFF 图片输入', ['R05','R06']),
    ('XLS 费用表', ['R07']), ('XLSX 多表与公式缓存', ['R07']),
    ('DOC 正文表格', ['R08']), ('DOCX 表格和段落', ['R08']),
    ('原文定位和严格序列化', ['R28','R29']), ('在线超时与模型畸形返回', ['R01','R06','R30']),
    ('打包安装及补充思考检查', ['R31','R32']),
]
VAL = [
    ('新布局多费用邮件', ['R02','R12','R17']), ('独立提单附件链', ['R03','R29']),
    ('独立扫描通知与条件条款', ['R04','R13']), ('独立图片费用单', ['R05','R06']),
    ('独立 XLS 费用布局', ['R07']), ('独立 XLSX 费用布局', ['R07']),
    ('独立 DOC 模板', ['R08']), ('独立 DOCX 模板', ['R08']),
    ('明确更新与扩展字段保留', ['R20','R21']), ('多键匹配与候选冲突', ['R19','R24']),
    ('重传及跨来源重复', ['R18','R23']), ('独立贷项与财务视角', ['R22','R25']),
    ('币种付款范围与日期未知', ['R10','R15','R16','R27']), ('OCR/模型故障和指令干扰', ['R26','R30']),
    ('干净配置完整批次', ['R01','R28','R29','R32','R33']),
]
HOLD = [
    ('留出邮件正文费用', ['R02','R12','R17']), ('留出附件格式识别', ['R03']),
    ('留出多页 PDF 账单', ['R04','R06']), ('留出图片版式', ['R05','R06']),
    ('留出旧版 Excel', ['R07']), ('留出新版 Excel', ['R07']),
    ('留出旧版 Word', ['R08']), ('留出新版 Word', ['R08']),
    ('留出明确字段更新', ['R19','R20','R21']), ('留出重传与费用行身份', ['R18','R23']),
    ('留出多候选与证据冲突', ['R24','R27']), ('留出双方记账及贷项', ['R22']),
    ('留出财务精度和付款范围', ['R13','R14','R15','R16','R25']),
    ('留出故障和 OCR 混淆', ['R26','R30']), ('留出完整运行与合法结果', ['R01','R09','R10','R11','R28','R29','R31','R32','R33']),
]


def main():
    index = []
    for split, specs in [('development', DEV), ('validation', VAL), ('holdout', HOLD)]:
        prefix = {'development':'DEV','validation':'VAL','holdout':'HOLD'}[split]
        for i, (title, requirements) in enumerate(specs, 1):
            cid = f'{prefix}-{i:03d}'
            index.append({'case_id': cid, 'family_id': f'synthetic-{cid.lower()}',
                          'split': split, 'source_kind': 'synthetic', 'title': title,
                          'requirements': requirements, 'status': 'planned', 'files': []})
    for cid, body, old, expected, authorized, status, after in build_cases():
        dest = BASE/'synthetic'/cid
        dest.mkdir(parents=True, exist_ok=True)
        msg = EmailMessage(policy=policy.SMTP)
        msg['From'] = 'fixture-sender@example.invalid'
        msg['To'] = 'fixture-reader@example.invalid'
        msg['Subject'] = f'SYNTHETIC {cid}'
        msg['Date'] = 'Tue, 20 Oct 2026 08:00:00 +0000'
        msg['Message-ID'] = f'<{cid.lower()}@example.invalid>'
        msg.set_content(body, charset='utf-8')
        raw = msg.as_bytes()
        (dest/'input.eml').write_bytes(raw)
        write(dest/'request.json', {'schema_version':'0.1.0',
              'documents':[{'source_id':f'{cid}-source','path':'input.eml','role':'business_document'}],
              'accounting_entity': None if cid=='DEV-008' else ORIGIN,
              'existing_bills': old, 'party_aliases': {}})
        write(dest/'expected.json', {'annotation_source':'authored_synthetic_not_prediction',
              'expected_records':expected, 'expected_file_status':status,
              'authorized_change_paths':authorized, 'expected_existing_snapshot_after':after,
              'comparison_ignore_paths':['/record_id','/audit_trail/ai_reasoning'],
              'can_auto_process':bool(expected) and status=='processed',
              'input_sha256':hashlib.sha256(raw).hexdigest()})
        item = next(x for x in index if x['case_id']==cid)
        item['family_id']='synthetic-dev-email-seed-v1'
        item['status']='materialized_not_executed'
        item['files']=[f'synthetic/{cid}/{n}' for n in ['input.eml','request.json','expected.json']]
    write(BASE/'scenario-catalog.json', {'schema_version':'0.1.0',
          'split_policy':'Source/template families must remain in one split; planned families are provisional until materialization and lineage review.',
          'holdout_policy':'Only coverage slots are listed; concrete holdout documents and answers are not yet authored and must not be used for prompt tuning.',
          'cases':index})
    print('Authored 12 synthetic EML fixtures and catalogued 60 scenario slots; no application tests executed.')


if __name__ == '__main__':
    main()
