"""核验验收案例引用与原文快照并导出阅读版；不执行任何 RAG 或模型请求。"""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CATALOG=ROOT/'eval/v1_final_acceptance_20261024.json'
OUT=ROOT/'reports/v1_flow_review_20261024'


def read(path):
    if path.suffix=='.jsonl':return [json.loads(t) for t in path.read_text(encoding='utf-8').splitlines()]
    return json.loads(path.read_text(encoding='utf-8'))


def resolve(ref):
    data=read(ROOT/ref['file'])
    if isinstance(data,dict):data=data['questions']
    return next(r for r in data if r.get('id',r.get('question_id'))==ref['id'])[ref['field']]


def main():
    catalog=read(CATALOG);errors=[];rows=[];checked=set()
    assert len(catalog['cases'])==catalog['case_count']==22
    assert len({c['id'] for c in catalog['cases']})==22
    for file,digest in catalog['source_snapshot'].items():
        if hashlib.sha256((ROOT/file).read_bytes()).hexdigest()!=digest:errors.append('研究来源快照变化：'+file)
    lines=['# DorisRAG V1 最终验收案例','',
      '22 张案例卡，复用既有诊断/留出/第二轮用例、已核查社区场景及 Ragas 候选；本轮未调用模型、未重新出题或联网研究。',
      '本文由 `eval/v1_final_acceptance_20261024.json` 导出。单题/变体每次新建会话；只有标记“连续对话”的案例保留状态。',
      '知识问答最终验收仍须新 768 维索引的真实 HTTP/UI/LLM/Dense 结果，当前引用核验不等于答案通过。','',
      '共同判据：默认仅4.x；本轮明确版本优先；不自动扩大范围；产品事实由允许范围官方文档支持；用户事实/规则/推论/未知分开。',
      '输入材料的历史范围字段不能覆盖上述新契约。无数值 SLA；记录客户端反馈、完整耗时、调用次数及冷/热状态。',
      '列出的官方依据是已核对锚点；替代文档须按同样版本/原文条件人工核实，不改写旧诊断集评分。澄清前未检索与范围错误分别判断。','']
    modes={'single':'单轮','variants':'独立变体（逐个新建会话）','conversation':'连续对话','ui_procedure':'页面操作/受控回放'}
    for c in catalog['cases']:
        for origin in c['origins']:
            if not (ROOT/origin['file']).is_file():errors.append(c['id']+'来源缺失')
        inputs=[]
        for step in c['inputs']:
            try:text=resolve(step['input_ref']) if 'input_ref' in step else step.get('input',step.get('action'))
            except (KeyError,StopIteration) as error:
                errors.append(c['id']+'输入引用失效');text='输入引用失效'
            assert isinstance(text,str) and text
            inputs.append(text)
        lines += ['## '+c['id']+' · '+c['title'],'',f"方式：{modes[c['mode']]}。预期来源范围：{c['expected_scope'] if c['expected_scope'] is not None else '见各步骤/判据'}。",'','输入：','']
        for i,(step,text) in enumerate(zip(c['inputs'],inputs),1):
            scope='；范围 '+','.join(step['expected_scope']) if step.get('expected_scope') else ''
            lines += [f'{i}. 输入'+scope,'','````text',text,'````','']
        lines += ['','预期与判定：','']+['- '+x for x in c['pass_conditions']]
        lines += ['','知识/行为依据：','']
        for b in c['knowledge_basis']:
            path=ROOT/b['file'];checked.add(b['file'])
            if not path.is_file():errors.append(c['id']+'依据缺失：'+b['file']);continue
            if b.get('sha256') and hashlib.sha256(path.read_bytes()).hexdigest()!=b['sha256']:errors.append(c['id']+'原文哈希变化：'+b['file'])
            if b.get('lines') and not 1<=b['lines'][0]<=b['lines'][1]<=len(path.read_text(encoding='utf-8-sig').splitlines()):errors.append(c['id']+'原文行范围越界')
            label=(b.get('source_version','产品契约')+' · '+path.name)
            location=':L'+str(b['lines'][0])+'–L'+str(b['lines'][1]) if b.get('lines') else ''
            lines.append(f"- [{label}]({path.as_posix()}){location}"+('；'+b['supports'] if b.get('supports') else ''))
        for o in c['origins']:lines.append('- 复用：`'+o['file']+'` → `'+o['id']+'`')
        if c.get('note'):lines+=['',c['note']]
        lines += ['','本轮状态：只整理/核验依据，未执行此案的全库问答；已有组件与页面结果另见检查报告。','']
        rows.append({'id':c['id'],'title':c['title'],'mode':c['mode'],'inputs':inputs,'final_requires_dense':c['final_requires_dense']})
    # Ragas 参考不是Gold；仅核对选中候选已有的官方来源映射和内容身份。
    maps={r['id']:r for r in read(ROOT/'experiments/ragas_20261023/final_run/evidence_map.json')}
    for qid in ['Q05','Q06','Q14','Q19','Q21']:
        for context in maps[qid]['contexts']:
            if not context.get('source_mapping_verified'):errors.append(qid+'旧来源映射未核验')
            for source in context['sources']:
                if hashlib.sha256(Path(source['original_path']).read_bytes()).hexdigest()!=source['source_sha256']:errors.append(qid+'旧来源已变化')
    lines+=['## 复用时排除的旧判据','']+['- `'+r['source']+'`：'+r['reason'] for r in catalog['reuse_exclusions']]
    (ROOT/'eval/V1_FINAL_ACCEPTANCE_20261024.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    report={'passed':not errors,'case_cards':len(rows),'referenced_basis_files':len(checked),'resolved_input_steps':sum(len(r['inputs']) for r in rows),
            'cases':rows,'errors':errors,'llm_calls':0,'dense_queries':0,'full_system_accepted':False}
    (OUT/'catalog_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=True))
    if errors:raise SystemExit(1)


if __name__=='__main__':main()
