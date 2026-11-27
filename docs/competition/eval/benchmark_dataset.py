#!/usr/bin/env python
"""Canonical synthetic redaction benchmark dataset (no real information).

Design notes:
- The dictionary covers ONLY "known" entities; person names / unregistered orgs
  / addresses are deliberately kept OUT so the "unknown" tier truly tests NER
  rather than "answer = dictionary" grading itself.
- The "unknown" tier deliberately mixes in hard cases NER also misses (suppliers
  without a "公司" suffix, org names that look like project names) so the number
  is not inflated.
- "Decoys" (clause numbers / page numbers / percentages / quantities / public
  statute references / generic terms) MUST NOT be redacted — they quantify
  over-redaction (precision side).

This module is the single source of truth. ``build_benchmark_fixture.py`` freezes
it into versioned JSONL/JSON fixtures consumed at runtime and in tests.
"""
from __future__ import annotations

# The dictionary covers only these "known" entities (other unknown entities are
# deliberately left out).
DICT_ENTRIES = [
    {"label": "ORG", "canonical_value": "示例建设集团"},
    {"label": "ORG", "canonical_value": "东方示例研究院"},
    {"label": "PROJECT", "canonical_value": "测试星河项目"},
    {"label": "PROJECT", "canonical_value": "启明测试工程"},
    {"label": "SUPPLIER", "canonical_value": "示范供应链有限公司"},
    {"label": "DEPARTMENT", "canonical_value": "法务测试部"},
    {"label": "ORG", "canonical_value": "星桥模拟控股"},
    {"label": "ORG", "canonical_value": "蓝湖测试基金"},
    {"label": "PROJECT", "canonical_value": "云岭模拟产业园"},
    {"label": "LAND_PARCEL", "canonical_value": "南岸样本地块"},
    {"label": "SUPPLIER", "canonical_value": "青禾测试材料"},
    {"label": "DEPARTMENT", "canonical_value": "合成风控委员会"},
    {"label": "PROJECT", "canonical_value": "星云示例平台"},
    {"label": "ORG", "canonical_value": "北辰测试制造"},
    {"label": "ORG", "canonical_value": "卓然模拟律所"},
    {"label": "DEPARTMENT", "canonical_value": "融景示例部"},
    {"label": "ORG", "canonical_value": "虚构星罗资本"},
    {"label": "SUPPLIER", "canonical_value": "虚构航标服务"},
    {"label": "PROJECT", "canonical_value": "银桐样本计划"},
    {"label": "PROJECT", "canonical_value": "虚构样板枢纽一期"},
    {"label": "PROJECT", "canonical_value": "样本港二期自动化线"},
]
DICT_ENTRIES = [
    {**e, "entry_id": f"k{i}", "aliases": [], "risk_level": "high",
     "status": "active", "version": "eval"}
    for i, e in enumerate(DICT_ENTRIES)
]

# Each item: (text, [(value, label, category)], [decoy, ...])
# category: formatted / known / unknown
S = [
    ("联系人王海洋，电话13900001111，邮箱wang@example.test。",
     [("王海洋","PERSON","unknown"),("13900001111","CONTACT","formatted"),("wang@example.test","CONTACT","formatted")], []),
    ("示例建设集团与合成供应商丙就测试星河项目签约，合同编号：HT-2027-AB-0099，金额为人民币2,500,000.00元。",
     [("示例建设集团","ORG","known"),("合成供应商丙","SUPPLIER","unknown"),("测试星河项目","PROJECT","known"),
      ("HT-2027-AB-0099","CONTRACT_ID","formatted"),("人民币2,500,000.00元","AMOUNT","formatted")], ["第3.2条"]),
    ("收款账号：6228480402564890018，签署日期2027年3月15日，违约金为合同金额的20%。",
     [("6228480402564890018","BANK_ACCOUNT","formatted"),("2027年3月15日","DATE","formatted")], ["合同金额的20%"]),
    ("统一社会信用代码91310000MA1FL0XY9K，系统地址https://erp.example.local/login。",
     [("91310000MA1FL0XY9K","ORG_CODE","formatted"),("https://erp.example.local/login","SYSTEM_URL","formatted")], []),
    ("配置项 api_key=sk-test_abcdef1234567890ghij 请勿外泄，本项目仅作演示。",
     [("sk-test_abcdef1234567890ghij","CREDENTIAL","formatted")], ["本项目"]),
    ("经办人李雪梅复核，监理单位为中诚信测试咨询有限公司。",
     [("李雪梅","PERSON","unknown"),("中诚信测试咨询有限公司","ORG","unknown")], []),
    ("示例建设集团项目部位于合成市示例区测试大道100号，质保期24个月。",
     [("示例建设集团","ORG","known"),("合成市示例区测试大道100号","ADDRESS","unknown")], ["质保期24个月"]),
    ("乙方代表赵国强，电话13700002222，就测试星河项目提出异议，占比15%。",
     [("赵国强","PERSON","unknown"),("13700002222","CONTACT","formatted"),("测试星河项目","PROJECT","known")], ["占比15%"]),
    ("协议编号：XY-2026-CG-1234，价款为800万元，付款日期2026年12月1日。",
     [("XY-2026-CG-1234","CONTRACT_ID","formatted"),("2026年12月1日","DATE","formatted")], ["价款为800万元"]),
    ("项目经理陈芳与合众示例工程监理有限公司沟通，邮箱li.mei@corp.example.cn。",
     [("陈芳","PERSON","unknown"),("合众示例工程监理有限公司","ORG","unknown"),("li.mei@corp.example.cn","CONTACT","formatted")], []),
    ("银行账号：6222 0202 0202 0202 020，开户行信息见附件，第5页。",
     [("6222 0202 0202 0202 020","BANK_ACCOUNT","formatted")], ["第5页"]),
    ("采购编号：ZB-2027-0001-A，供应商为远大测试建材公司，报价1500万元。",
     [("ZB-2027-0001-A","CONTRACT_ID","formatted"),("远大测试建材公司","SUPPLIER","unknown")], ["报价1500万元"]),
    ("刘建国主任审批，部门为法务测试部，系统地址http://oa.demo.example.cn/flow。",
     [("刘建国","PERSON","unknown"),("法务测试部","DEPARTMENT","known"),("http://oa.demo.example.cn/flow","SYSTEM_URL","formatted")], []),
    ("示范供应链有限公司承接启明测试工程，合同金额￥1,200,000元。",
     [("示范供应链有限公司","SUPPLIER","known"),("启明测试工程","PROJECT","known"),("￥1,200,000元","AMOUNT","formatted")], []),
    ("收款人周敏，账户621700123456789，到账日期2027-03-20。",
     [("周敏","PERSON","unknown"),("621700123456789","BANK_ACCOUNT","formatted"),("2027-03-20","DATE","formatted")], []),
    ("东方示例研究院联系人孙立，电话15812345678，地址示范省样板市虚拟路25号。",
     [("东方示例研究院","ORG","known"),("孙立","PERSON","unknown"),("15812345678","CONTACT","formatted"),
      ("示范省样板市虚拟路25号","ADDRESS","unknown")], []),
    ("token: ghp_TestToken1234567890abcdEFGH，请在测试星河项目内部使用。",
     [("ghp_TestToken1234567890abcdEFGH","CREDENTIAL","formatted"),("测试星河项目","PROJECT","known")], []),
    ("吴婷婷与嘉禾测试设计院核对图纸，编号规则另行约定。",
     [("吴婷婷","PERSON","unknown"),("嘉禾测试设计院","ORG","unknown")], ["编号规则"]),
    ("信用代码913502001234567XQ8，邮箱zhao_g@vendor.example.org，共5台设备。",
     [("913502001234567XQ8","ORG_CODE","formatted"),("zhao_g@vendor.example.org","CONTACT","formatted")], ["共5台设备"]),
    ("郑海代表中邮示例证券出席，电话17066668888。",
     [("郑海","PERSON","unknown"),("中邮示例证券","ORG","unknown"),("17066668888","CONTACT","formatted")], []),
    ("招标编号：CG-2027-PUR-0456，中标单位西部示例建工集团，金额为3200万元。",
     [("CG-2027-PUR-0456","CONTRACT_ID","formatted"),("西部示例建工集团","ORG","unknown")], ["金额为3200万元"]),
    ("黄文博签署，secret=AKIATESTKEY1234567890ABCD，存于法务测试部。",
     [("黄文博","PERSON","unknown"),("AKIATESTKEY1234567890ABCD","CREDENTIAL","formatted"),("法务测试部","DEPARTMENT","known")], []),
    ("示例建设集团与信达测试评估事务所就启明测试工程评估，日期2027/06/30。",
     [("示例建设集团","ORG","known"),("信达测试评估事务所","ORG","unknown"),("启明测试工程","PROJECT","known"),("2027/06/30","DATE","formatted")], []),
    ("林志远负责对接，收款账号6217003812345678901，担保比例30%。",
     [("林志远","PERSON","unknown"),("6217003812345678901","BANK_ACCOUNT","formatted")], ["担保比例30%"]),
    ("项目地址合成市开发区样例广场A座，联系人何晓，电话18512340000。",
     [("合成市开发区样例广场A座","ADDRESS","unknown"),("何晓","PERSON","unknown"),("18512340000","CONTACT","formatted")], []),
    ("合同编号：HT-2027-DD-7788，对方为恒通示例物流，报价为600万元。",
     [("HT-2027-DD-7788","CONTRACT_ID","formatted"),("恒通示例物流","SUPPLIER","unknown")], ["报价为600万元"]),
    ("罗建华与鼎盛示例机电协商，系统https://vpn.example.local:8443/portal。",
     [("罗建华","PERSON","unknown"),("鼎盛示例机电","ORG","unknown"),("https://vpn.example.local:8443/portal","SYSTEM_URL","formatted")], []),
    ("示范供应链有限公司开票，金额¥99,800元，税率9%。",
     [("示范供应链有限公司","SUPPLIER","known"),("¥99,800元","AMOUNT","formatted")], ["税率9%"]),
    ("高萍核对账户6228480488889990001，开户日期2026年9月9日。",
     [("高萍","PERSON","unknown"),("6228480488889990001","BANK_ACCOUNT","formatted"),("2026年9月9日","DATE","formatted")], []),
    ("马俊杰代表三川示例分包队进场，工期为90天。",
     [("马俊杰","PERSON","unknown"),("三川示例分包队","SUPPLIER","unknown")], ["工期为90天"]),
    ("东方示例研究院唐磊提交报告，邮箱contact@demo.example.io。",
     [("东方示例研究院","ORG","known"),("唐磊","PERSON","unknown"),("contact@demo.example.io","CONTACT","formatted")], []),
    ("信用代码91440300MA5XYZ12AB，地址测试新区模拟街88栋。",
     [("91440300MA5XYZ12AB","ORG_CODE","formatted"),("测试新区模拟街88栋","ADDRESS","unknown")], []),
    ("冯丽与华城测试地产洽谈，电话19911112222，《民法典》第五百八十五条。",
     [("冯丽","PERSON","unknown"),("华城测试地产","ORG","unknown"),("19911112222","CONTACT","formatted")], ["《民法典》第五百八十五条"]),
    ("协议编号：XY-2027-LV-2200，劳务队为永固测试劳务，金额为450万元。",
     [("XY-2027-LV-2200","CONTRACT_ID","formatted"),("永固测试劳务","SUPPLIER","unknown")], ["金额为450万元"]),
    ("董强主持会议，启明测试工程进度见第3章，共计3次变更。",
     [("董强","PERSON","unknown"),("启明测试工程","PROJECT","known")], ["第3章","共计3次变更"]),
    ("收款人韩雪，银行账号6217009900112233445，api-key = pk_test_0987654321zyxwvu。",
     [("韩雪","PERSON","unknown"),("6217009900112233445","BANK_ACCOUNT","formatted"),("pk_test_0987654321zyxwvu","CREDENTIAL","formatted")], []),
    ("曹斌与鑫源样板物资签订，采购编号CG-2026-MAT-0099，单价见综合单价表。",
     [("曹斌","PERSON","unknown"),("CG-2026-MAT-0099","CONTRACT_ID","formatted")], ["综合单价表"]),
    ("示例建设集团法务测试部彭宇审核，日期2027-1-5。",
     [("示例建设集团","ORG","known"),("法务测试部","DEPARTMENT","known"),("彭宇","PERSON","unknown"),("2027-1-5","DATE","formatted")], []),
    ("许文代表中建示例第九工程局，电话13612349999，地址样板市测试路9号。",
     [("许文","PERSON","unknown"),("中建示例第九工程局","ORG","unknown"),("13612349999","CONTACT","formatted"),("样板市测试路9号","ADDRESS","unknown")], []),
    ("报价为1800万元，供应商袁莉所在的瑞丰测试装饰有限公司提交。",
     [("袁莉","PERSON","unknown"),("瑞丰测试装饰有限公司","SUPPLIER","unknown")], ["报价为1800万元"]),
    ("邮箱info@sub.example.test，系统https://api.sub.example.test/v1，测试星河项目专用。",
     [("info@sub.example.test","CONTACT","formatted"),("https://api.sub.example.test/v1","SYSTEM_URL","formatted"),("测试星河项目","PROJECT","known")], []),
    ("叶明与天成示例咨询核对，信用代码91330100MA2A1B3C4D。",
     [("叶明","PERSON","unknown"),("天成示例咨询","ORG","unknown"),("91330100MA2A1B3C4D","ORG_CODE","formatted")], []),
    ("蒋涛负责，收款账户6228999988887776660，金额人民币￥3,680,000.50元。",
     [("蒋涛","PERSON","unknown"),("6228999988887776660","BANK_ACCOUNT","formatted"),("人民币￥3,680,000.50元","AMOUNT","formatted")], []),
    ("示范供应链有限公司联系人于洋，电话15999998888，一式四份。",
     [("示范供应链有限公司","SUPPLIER","known"),("于洋","PERSON","unknown"),("15999998888","CONTACT","formatted")], ["一式四份"]),
    ("合同编号HT-2026-SV-3311，对方安泰测试安保服务，日期2026/8/8。",
     [("HT-2026-SV-3311","CONTRACT_ID","formatted"),("安泰测试安保服务","SUPPLIER","unknown"),("2026/8/8","DATE","formatted")], []),
    ("东方示例研究院钱进出席，地址合成市江北区示范大厦18层。",
     [("东方示例研究院","ORG","known"),("钱进","PERSON","unknown"),("合成市江北区示范大厦18层","ADDRESS","unknown")], []),
    ("token=sk-live_TESTonly000111222333，仅限启明测试工程，第二标段。",
     [("sk-live_TESTonly000111222333","CREDENTIAL","formatted"),("启明测试工程","PROJECT","known")], ["第二标段"]),
    ("沈括与宏图示例规划设计院对接，电话13388887777。",
     [("沈括","PERSON","unknown"),("宏图示例规划设计院","ORG","unknown"),("13388887777","CONTACT","formatted")], []),
    ("采购编号CG-2027-EQ-5050，金额为2600万元，供应商正合测试设备。",
     [("CG-2027-EQ-5050","CONTRACT_ID","formatted"),("正合测试设备","SUPPLIER","unknown")], ["金额为2600万元"]),
    ("示例建设集团贾敏审批，邮箱jia@example.test，日期2027年7月1日。",
     [("示例建设集团","ORG","known"),("贾敏","PERSON","unknown"),("jia@example.test","CONTACT","formatted"),("2027年7月1日","DATE","formatted")], []),
    ("信用代码91500000MA6Q7R8S9T，对方为联兴测试供应链管理有限公司。",
     [("91500000MA6Q7R8S9T","ORG_CODE","formatted"),("联兴测试供应链管理有限公司","ORG","unknown")], []),
    ("收款账号6217005566778899001，金额￥58,000元，质量保证金占5%。",
     [("6217005566778899001","BANK_ACCOUNT","formatted"),("￥58,000元","AMOUNT","formatted")], ["质量保证金占5%"]),
    ("武磊与德盛示例钢构联络，电话17799990000，地址测试县样例镇中心街3号。",
     [("武磊","PERSON","unknown"),("德盛示例钢构","SUPPLIER","unknown"),("17799990000","CONTACT","formatted"),("测试县样例镇中心街3号","ADDRESS","unknown")], []),
    ("启明测试工程由方圆测试造价咨询有限公司审价，编号见正文。",
     [("启明测试工程","PROJECT","known"),("方圆测试造价咨询有限公司","ORG","unknown")], ["编号见正文"]),
    ("秦风提交，secret = TESTsecret_ABCD1234efgh5678，日期2026-11-11。",
     [("秦风","PERSON","unknown"),("TESTsecret_ABCD1234efgh5678","CREDENTIAL","formatted"),("2026-11-11","DATE","formatted")], []),
    ("示范供应链有限公司与海川测试机械租赁结算，金额为720万元。",
     [("示范供应链有限公司","SUPPLIER","known"),("海川测试机械租赁","SUPPLIER","unknown")], ["金额为720万元"]),
    ("协议编号XY-2026-CN-0808，对方崔颢，电话13900007777，第4页。",
     [("XY-2026-CN-0808","CONTRACT_ID","formatted"),("崔颢","PERSON","unknown"),("13900007777","CONTACT","formatted")], ["第4页"]),
    ("东方示例研究院与新元测试环保科技有限公司合作，邮箱ops@new.example.cn。",
     [("东方示例研究院","ORG","known"),("新元测试环保科技有限公司","ORG","unknown"),("ops@new.example.cn","CONTACT","formatted")], []),
    ("尤勇核对账户6228770011223344556，报价900万元，增值税率9%。",
     [("尤勇","PERSON","unknown"),("6228770011223344556","BANK_ACCOUNT","formatted")], ["报价900万元","增值税率9%"]),
    ("测试星河项目监理为顺达测试监理，联系人葛亮，电话15700001234。",
     [("测试星河项目","PROJECT","known"),("顺达测试监理","SUPPLIER","unknown"),("葛亮","PERSON","unknown"),("15700001234","CONTACT","formatted")], []),
]

ADDITIONAL_S = [
    (
        "投前摘要：蓝湖测试基金拟投云岭模拟产业园，金额为人民币32亿元，位置在合成省东湾新区，推进节点为2027年二季度签约；内部意见：建议暂缓披露底价策略。",
        [("蓝湖测试基金", "ORG", "known"), ("云岭模拟产业园", "PROJECT", "known"), ("人民币32亿元", "AMOUNT", "formatted"), ("合成省东湾新区", "LOCATION", "quasi_identifier"), ("2027年二季度签约", "MILESTONE", "quasi_identifier"), ("建议暂缓披露底价策略", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "pre_investment", "investment_memo"
    ),
    (
        "投前访谈记录：联络人顾晨使用邮箱gu.chen@example.test，提到未登记项目名银桐样本计划，目标规模约18万平方米，风险判断为现金流压力高于模型假设。",
        [("顾晨", "PERSON", "unknown"), ("gu.chen@example.test", "CONTACT", "formatted"), ("银桐样本计划", "PROJECT", "unknown"), ("约18万平方米", "SCALE", "quasi_identifier"), ("现金流压力高于模型假设", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "pre_investment", "interview_note"
    ),
    (
        "投前清单：星桥模拟控股与虚构方舟开发谈判，合同编号INV-2027-PI-0101，初步下浮率为7.5%，谈判底线不得低于净现值中性方案。",
        [("星桥模拟控股", "ORG", "known"), ("虚构方舟开发", "ORG", "unknown"), ("INV-2027-PI-0101", "CONTRACT_ID", "formatted"), ("7.5%", "DISCOUNT_RATE", "formatted"), ("谈判底线不得低于净现值中性方案", "NEGOTIATION_FLOOR", "semantic_sensitive")],
        [], "pre_investment", "investment_checklist"
    ),
    (
        "投前风控：南岸样本地块预计2027/09/15完成摘牌，周边三公里内只有一个替代标的，内部意见为先锁定排他期再上会。",
        [("南岸样本地块", "LAND_PARCEL", "known"), ("2027/09/15", "DATE", "formatted"), ("周边三公里内只有一个替代标的", "LOCATION_CONTEXT", "quasi_identifier"), ("先锁定排他期再上会", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "pre_investment", "risk_note"
    ),
    (
        "投前模型：北辰测试制造拟作为产业方，电话18800001111，目标退出窗口为30至36个月，法律策略是保留回购触发权。",
        [("北辰测试制造", "ORG", "known"), ("18800001111", "CONTACT", "formatted"), ("30至36个月", "TIMELINE", "quasi_identifier"), ("保留回购触发权", "LEGAL_STRATEGY", "semantic_sensitive")],
        [], "pre_investment", "model_note"
    ),
    (
        "投前补充：未登记顾问辰光测试咨询提交URL https://memo.example.local/pi/alpha，称示范片区A-3单元可研敏感，暂不向外部顾问开放。",
        [("辰光测试咨询", "ORG", "unknown"), ("https://memo.example.local/pi/alpha", "SYSTEM_URL", "formatted"), ("示范片区A-3单元", "LOCATION", "quasi_identifier"), ("暂不向外部顾问开放", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "pre_investment", "supplement"
    ),
    (
        "投前纪要：蓝湖测试基金、青禾测试材料讨论供货锁价，金额￥4,800,000元，推进节点是取得模拟环评批复后五日内签署。",
        [("蓝湖测试基金", "ORG", "known"), ("青禾测试材料", "SUPPLIER", "known"), ("￥4,800,000元", "AMOUNT", "formatted"), ("取得模拟环评批复后五日内签署", "MILESTONE", "quasi_identifier")],
        [], "pre_investment", "meeting_extract"
    ),
    (
        "投前邮件：收件人林沐，邮箱lin.mu@example.test，附件口径写明预案B只给内部评审会，不给交易对方。",
        [("林沐", "PERSON", "unknown"), ("lin.mu@example.test", "CONTACT", "formatted"), ("预案B只给内部评审会，不给交易对方", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "pre_investment", "email_extract"
    ),
    (
        "投前审批：合成风控委员会要求对云岭模拟产业园增加担保，信用代码91360000TESTPI0001，仅用于合成样本。",
        [("合成风控委员会", "DEPARTMENT", "known"), ("云岭模拟产业园", "PROJECT", "known"), ("91360000TESTPI0001", "ORG_CODE", "formatted")],
        [], "pre_investment", "approval_note"
    ),
    (
        "投前退出：虚构星罗资本报价人民币9.6亿元，预计窗口为2028年上半年，风险判断为退出依赖单一买方。",
        [("虚构星罗资本", "ORG", "unknown"), ("人民币9.6亿元", "AMOUNT", "formatted"), ("2028年上半年", "TIMELINE", "quasi_identifier"), ("退出依赖单一买方", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "pre_investment", "exit_note"
    ),
    (
        "会议纪要：合成风控委员会在2027年4月3日讨论星云示例平台，联系人邵宁，结论为暂不披露供应商替换方案。",
        [("合成风控委员会", "DEPARTMENT", "known"), ("2027年4月3日", "DATE", "formatted"), ("星云示例平台", "PROJECT", "known"), ("邵宁", "PERSON", "unknown"), ("暂不披露供应商替换方案", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "meeting_note", "committee_minutes"
    ),
    (
        "会议纪要：测试北楼3层小会议室形成意见，电话13600003333，内部判断为先压低首轮报价再释放备选条款。",
        [("测试北楼3层小会议室", "LOCATION", "quasi_identifier"), ("13600003333", "CONTACT", "formatted"), ("先压低首轮报价再释放备选条款", "NEGOTIATION_STRATEGY", "semantic_sensitive")],
        [], "meeting_note", "negotiation_minutes"
    ),
    (
        "会议纪要：未登记机构澄明样本评估到场，账号6228480400001111222，会议建议为删除对外材料中的失败试算。",
        [("澄明样本评估", "ORG", "unknown"), ("6228480400001111222", "BANK_ACCOUNT", "formatted"), ("删除对外材料中的失败试算", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "meeting_note", "review_minutes"
    ),
    (
        "会议纪要：示例建设集团讨论南岸样本地块，推进节点为下周三前完成模拟尽调，风险判断为舆情窗口偏窄。",
        [("示例建设集团", "ORG", "known"), ("南岸样本地块", "LAND_PARCEL", "known"), ("下周三前完成模拟尽调", "MILESTONE", "quasi_identifier"), ("舆情窗口偏窄", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "meeting_note", "due_diligence_minutes"
    ),
    (
        "会议纪要：许知远负责会后联络，邮箱xuzhiyuan@example.test，敏感结论为不主动提示对方违约豁免空间。",
        [("许知远", "PERSON", "unknown"), ("xuzhiyuan@example.test", "CONTACT", "formatted"), ("不主动提示对方违约豁免空间", "LEGAL_STRATEGY", "semantic_sensitive")],
        [], "meeting_note", "legal_minutes"
    ),
    (
        "会议纪要：东方示例研究院同步接口 https://ops.example.local/board，节点为A轮材料冻结前一小时，内部意见为仅同步摘要。",
        [("东方示例研究院", "ORG", "known"), ("https://ops.example.local/board", "SYSTEM_URL", "formatted"), ("A轮材料冻结前一小时", "MILESTONE", "quasi_identifier"), ("仅同步摘要", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "meeting_note", "ops_minutes"
    ),
    (
        "会议纪要：融景示例部与未登记供应商岭南样本运维确认，金额为860万元，谈判底线为不接受预付款比例上调。",
        [("融景示例部", "DEPARTMENT", "known"), ("岭南样本运维", "SUPPLIER", "unknown"), ("860万元", "AMOUNT", "formatted"), ("不接受预付款比例上调", "NEGOTIATION_FLOOR", "semantic_sensitive")],
        [], "meeting_note", "supplier_minutes"
    ),
    (
        "会议纪要：会议号MN-2027-TEST-0021记录星桥模拟控股发言，地点为合成港西区二号楼，风险判断为单点审批依赖明显。",
        [("MN-2027-TEST-0021", "CONTRACT_ID", "formatted"), ("星桥模拟控股", "ORG", "known"), ("合成港西区二号楼", "LOCATION", "quasi_identifier"), ("单点审批依赖明显", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "meeting_note", "board_minutes"
    ),
    (
        "会议纪要：秘书韩语桐记录token=TESTmeetingToken12345678，内部意见为待对方先出书面承诺后再回复。",
        [("韩语桐", "PERSON", "unknown"), ("TESTmeetingToken12345678", "CREDENTIAL", "formatted"), ("待对方先出书面承诺后再回复", "LEGAL_STRATEGY", "semantic_sensitive")],
        [], "meeting_note", "secretary_note"
    ),
    (
        "会议纪要：云岭模拟产业园预计容量12万千瓦，2027年三季度试运行，底线策略为不承诺独家采购。",
        [("云岭模拟产业园", "PROJECT", "known"), ("12万千瓦", "SCALE", "quasi_identifier"), ("2027年三季度试运行", "MILESTONE", "quasi_identifier"), ("不承诺独家采购", "NEGOTIATION_FLOOR", "semantic_sensitive")],
        [], "meeting_note", "project_minutes"
    ),
    (
        "法律意见：卓然模拟律所认为XY-2027-LAW-0007项下可主张解除，内部策略为先发保留权利函而非直接起诉。",
        [("卓然模拟律所", "ORG", "known"), ("XY-2027-LAW-0007", "CONTRACT_ID", "formatted"), ("先发保留权利函而非直接起诉", "LEGAL_STRATEGY", "semantic_sensitive")],
        [], "legal_opinion", "legal_opinion"
    ),
    (
        "法律意见：承办人孟然，电话15500004444，判断对方在2027年5月10日前补正可降低解除风险。",
        [("孟然", "PERSON", "unknown"), ("15500004444", "CONTACT", "formatted"), ("2027年5月10日前", "DATE", "formatted"), ("补正可降低解除风险", "LEGAL_STRATEGY", "semantic_sensitive")],
        [], "legal_opinion", "legal_memo"
    ),
    (
        "法律意见：未登记相对方景澜样本建设涉及合成省北川片区，争议金额人民币1,300,000元，风险判断为证据链存在断点。",
        [("景澜样本建设", "ORG", "unknown"), ("合成省北川片区", "LOCATION", "quasi_identifier"), ("人民币1,300,000元", "AMOUNT", "formatted"), ("证据链存在断点", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "legal_opinion", "dispute_note"
    ),
    (
        "法律意见：示范供应链有限公司案号LAW-2027-SYN-010，策略意见为保留反诉但暂不在首轮函件披露。",
        [("示范供应链有限公司", "SUPPLIER", "known"), ("LAW-2027-SYN-010", "CONTRACT_ID", "formatted"), ("保留反诉但暂不在首轮函件披露", "LEGAL_STRATEGY", "semantic_sensitive")],
        [], "legal_opinion", "case_strategy"
    ),
    (
        "法律意见：系统地址https://law.example.local/case/77，法务测试部备注底线为不接受无限连带保证。",
        [("https://law.example.local/case/77", "SYSTEM_URL", "formatted"), ("法务测试部", "DEPARTMENT", "known"), ("不接受无限连带保证", "NEGOTIATION_FLOOR", "semantic_sensitive")],
        [], "legal_opinion", "system_note"
    ),
    (
        "法律意见：联系人郁晴邮箱yuqing@example.test，案件地点为虚构市临港样本园，风险判断为管辖抗辩胜率偏低。",
        [("郁晴", "PERSON", "unknown"), ("yuqing@example.test", "CONTACT", "formatted"), ("虚构市临港样本园", "LOCATION", "quasi_identifier"), ("管辖抗辩胜率偏低", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "legal_opinion", "jurisdiction_note"
    ),
    (
        "法律意见：北辰测试制造与辰砂样本贸易发生质量争议，金额为2400万元，内部意见为先冻结验收口径。",
        [("北辰测试制造", "ORG", "known"), ("辰砂样本贸易", "ORG", "unknown"), ("2400万元", "AMOUNT", "formatted"), ("先冻结验收口径", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "legal_opinion", "quality_dispute"
    ),
    (
        "法律意见：信用代码91370000TESTLAW002，地址合成市样例路66号，策略为用证据保全倒逼对方提交台账。",
        [("91370000TESTLAW002", "ORG_CODE", "formatted"), ("合成市样例路66号", "LOCATION", "unknown"), ("用证据保全倒逼对方提交台账", "LEGAL_STRATEGY", "semantic_sensitive")],
        [], "legal_opinion", "evidence_note"
    ),
    (
        "法律意见：南岸样本地块相关协议预计2028/01/20到期，内部判断为若延期超过45天需触发专项汇报。",
        [("南岸样本地块", "LAND_PARCEL", "known"), ("2028/01/20", "DATE", "formatted"), ("延期超过45天", "MILESTONE", "quasi_identifier"), ("需触发专项汇报", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "legal_opinion", "deadline_note"
    ),
    (
        "法律意见：律师助理苏棠记录secret=TESTlegalSecret987654321，结论草案为不建议在对外版本写明胜诉概率。",
        [("苏棠", "PERSON", "unknown"), ("TESTlegalSecret987654321", "CREDENTIAL", "formatted"), ("不建议在对外版本写明胜诉概率", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "legal_opinion", "draft_note"
    ),
    (
        "风险汇总：星桥模拟控股本季潜亏25亿元，主要集中在合成东线仓储带，内部判断为不得在月报外传。",
        [("星桥模拟控股", "ORG", "known"), ("25亿元", "AMOUNT", "formatted"), ("合成东线仓储带", "LOCATION", "quasi_identifier"), ("不得在月报外传", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "risk_summary", "risk_summary"
    ),
    (
        "风险汇总：云岭模拟产业园现金缺口约4.2亿元，窗口期为2027年8月至10月，风险判断为再融资依赖单一渠道。",
        [("云岭模拟产业园", "PROJECT", "known"), ("约4.2亿元", "AMOUNT", "formatted"), ("2027年8月至10月", "TIMELINE", "quasi_identifier"), ("再融资依赖单一渠道", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "risk_summary", "cashflow_summary"
    ),
    (
        "风险汇总：未登记主体晟云样本运营上报电话16600005555，指标显示三周内可能触发模拟预警线。",
        [("晟云样本运营", "ORG", "unknown"), ("16600005555", "CONTACT", "formatted"), ("三周内可能触发模拟预警线", "MILESTONE", "quasi_identifier")],
        [], "risk_summary", "operation_summary"
    ),
    (
        "风险汇总：合成风控委员会认为星云示例平台若日活低于8万将影响估值，内部意见为暂不下调公开目标。",
        [("合成风控委员会", "DEPARTMENT", "known"), ("星云示例平台", "PROJECT", "known"), ("日活低于8万", "SCALE", "quasi_identifier"), ("暂不下调公开目标", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "risk_summary", "platform_summary"
    ),
    (
        "风险汇总：账号6222000011112222333对应保证金池，金额人民币5800万元，风险判断为池内集中度过高。",
        [("6222000011112222333", "BANK_ACCOUNT", "formatted"), ("人民币5800万元", "AMOUNT", "formatted"), ("池内集中度过高", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "risk_summary", "fund_summary"
    ),
    (
        "风险汇总：北辰测试制造在样本县北站片区库存约11万件，推进节点为月底前完成二次盘点。",
        [("北辰测试制造", "ORG", "known"), ("样本县北站片区", "LOCATION", "quasi_identifier"), ("约11万件", "SCALE", "quasi_identifier"), ("月底前完成二次盘点", "MILESTONE", "quasi_identifier")],
        [], "risk_summary", "inventory_summary"
    ),
    (
        "风险汇总：接口https://risk.example.local/internal显示南岸样本地块评分下降，内部策略为不触发外部评级沟通。",
        [("https://risk.example.local/internal", "SYSTEM_URL", "formatted"), ("南岸样本地块", "LAND_PARCEL", "known"), ("不触发外部评级沟通", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "risk_summary", "rating_summary"
    ),
    (
        "风险汇总：联系人白芷邮箱baizhi@example.test，称模拟园区B7栋停工超过20天，建议优先压降舆情暴露。",
        [("白芷", "PERSON", "unknown"), ("baizhi@example.test", "CONTACT", "formatted"), ("模拟园区B7栋", "LOCATION", "quasi_identifier"), ("停工超过20天", "MILESTONE", "quasi_identifier"), ("优先压降舆情暴露", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "risk_summary", "site_summary"
    ),
    (
        "风险汇总：合同编号RSK-2027-0008覆盖青禾测试材料，底线为不接受新增交叉违约条款。",
        [("RSK-2027-0008", "CONTRACT_ID", "formatted"), ("青禾测试材料", "SUPPLIER", "known"), ("不接受新增交叉违约条款", "NEGOTIATION_FLOOR", "semantic_sensitive")],
        [], "risk_summary", "contract_risk"
    ),
    (
        "风险汇总：虚构航标服务报价￥760,000元，位置为合成港东闸口，风险判断为替换窗口只有十个工作日。",
        [("虚构航标服务", "SUPPLIER", "unknown"), ("￥760,000元", "AMOUNT", "formatted"), ("合成港东闸口", "LOCATION", "quasi_identifier"), ("替换窗口只有十个工作日", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "risk_summary", "supplier_risk"
    ),
    (
        "HR画像：候选人何清，电话17300006666，邮箱heqing@example.test，曾负责虚构样板枢纽一期，内部意见为暂不披露薪酬上限。",
        [("何清", "PERSON", "unknown"), ("17300006666", "CONTACT", "formatted"), ("heqing@example.test", "CONTACT", "formatted"), ("虚构样板枢纽一期", "PROJECT", "quasi_identifier"), ("暂不披露薪酬上限", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "hr_profile", "candidate_profile"
    ),
    (
        "HR画像：员工编号EMP-TEST-2027-001，居住地样本市西桥社区，风险判断为岗位替代性低。",
        [("EMP-TEST-2027-001", "EMPLOYEE_ID", "formatted"), ("样本市西桥社区", "LOCATION", "quasi_identifier"), ("岗位替代性低", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "hr_profile", "employee_profile"
    ),
    (
        "HR画像：周砚参与星云示例平台核心模块，预计2027年6月离岗，内部策略为先做权限降级再沟通。",
        [("周砚", "PERSON", "unknown"), ("星云示例平台", "PROJECT", "known"), ("2027年6月离岗", "MILESTONE", "quasi_identifier"), ("先做权限降级再沟通", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "hr_profile", "departure_profile"
    ),
    (
        "HR画像：银行卡6225888800001111222用于合成薪酬测试，候选人来自未登记机构禾木样本咨询。",
        [("6225888800001111222", "BANK_ACCOUNT", "formatted"), ("禾木样本咨询", "ORG", "unknown")],
        [], "hr_profile", "payroll_profile"
    ),
    (
        "HR画像：融景示例部面试官唐微记录证件号11010119990101999X，结论为不在外部流程中讨论家庭情况。",
        [("融景示例部", "DEPARTMENT", "known"), ("唐微", "PERSON", "unknown"), ("11010119990101999X", "ID_CARD", "formatted"), ("不在外部流程中讨论家庭情况", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "hr_profile", "interview_profile"
    ),
    (
        "HR画像：候选人简历链接https://hr.example.local/candidate/42，过往项目位于合成市南港片区，规模约300人团队。",
        [("https://hr.example.local/candidate/42", "SYSTEM_URL", "formatted"), ("合成市南港片区", "LOCATION", "quasi_identifier"), ("约300人团队", "SCALE", "quasi_identifier")],
        [], "hr_profile", "resume_profile"
    ),
    (
        "HR画像：蓝湖测试基金外派岗联系人许苒，电话18000007777，内部判断为背景核验需人工复核。",
        [("蓝湖测试基金", "ORG", "known"), ("许苒", "PERSON", "unknown"), ("18000007777", "CONTACT", "formatted"), ("背景核验需人工复核", "RISK_JUDGMENT", "semantic_sensitive")],
        [], "hr_profile", "background_profile"
    ),
    (
        "HR画像：账号hr_key=TESThrKey1234567890abcd仅为合成样本，候选人叶舟希望在2027/07/31前确认。",
        [("TESThrKey1234567890abcd", "CREDENTIAL", "formatted"), ("叶舟", "PERSON", "unknown"), ("2027/07/31", "DATE", "formatted")],
        [], "hr_profile", "offer_profile"
    ),
    (
        "HR画像：北辰测试制造借调人员涉及样本港二期自动化线，内部意见为不共享绩效低分原因。",
        [("北辰测试制造", "ORG", "known"), ("样本港二期自动化线", "PROJECT", "quasi_identifier"), ("不共享绩效低分原因", "INTERNAL_OPINION", "semantic_sensitive")],
        [], "hr_profile", "assignment_profile"
    ),
    (
        "HR画像：联系人陆乔邮箱luqiao@example.test，面试地点为示例园A座18层，谈判底线为总包不超过合成薪级M3。",
        [("陆乔", "PERSON", "unknown"), ("luqiao@example.test", "CONTACT", "formatted"), ("示例园A座18层", "LOCATION", "quasi_identifier"), ("总包不超过合成薪级M3", "NEGOTIATION_FLOOR", "semantic_sensitive")],
        [], "hr_profile", "offer_negotiation"
    ),
]

S.extend(ADDITIONAL_S)


# Decoy sentences: every substring below MUST NOT be redacted (clause numbers /
# page numbers / percentages / quantities / public statute references / generic
# terms / durations). Each decoy is a literal substring of its sentence and
# quantifies over-redaction (precision).
DECOYS = [
    ("本合同共5页，第3.2条约定质保期24个月。", ["共5页", "第3.2条", "质保期24个月"]),
    ("依据《民法典》第585条，违约金为合同金额的20%。", ["《民法典》", "第585条", "违约金为合同金额的20%"]),
    ("担保比例30%，增值税率9%，质量保证金占5%。", ["担保比例30%", "增值税率9%", "质量保证金占5%"]),
    ("共5台设备、200吨钢材，工期为90天。", ["共5台设备", "200吨钢材", "工期为90天"]),
    ("综合单价见附表，本合同一式四份，详见第3章。", ["综合单价", "一式四份", "第3章"]),
    ("本项目第二标段由该公司承接，占比15%。", ["本项目", "第二标段", "该公司", "占比15%"]),
    ("编号规则另行约定，报价共计3次调整。", ["编号规则", "共计3次"]),
]
