"""Account checks through WebBridge; never read credentials, cookies or tokens."""
import http.client
import json
import time

IDENTITY_JS = r"""(() => {
 const browser = /Edg\//.test(navigator.userAgent) ? 'edge' :
   /Chrome\//.test(navigator.userAgent) && !/OPR\//.test(navigator.userAgent) ? 'chrome' : 'unknown';
 if (location.hostname !== 'www.zhipin.com') return JSON.stringify({browser, identity:null});
 const own = document.querySelector('#header .nav-figure,#header .nav-user,#header .user-nav');
 if (!own) return JSON.stringify({browser, identity:null});
 const candidates=[];
 for (let el=own, n=0; el && n<6; el=el.parentElement,n++) {
   const vm=el.__vue__;
   if (!vm) continue;
   for (const root of [vm.$data||{},vm.$store?.state||{}]) {
     for (const key of ['userInfo','user','geekInfo']) {
       const value=root[key];
       if(value && typeof value==='object') candidates.push(value.userInfo||value);
     }
   }
 }
 for(const u of candidates) {
   const id=u.encryptUserId||u.userId||u.uid;
   if((typeof id==='string'||typeof id==='number') && String(id) && String(id)!=='0')
     return JSON.stringify({browser,identity:{kind:'user_id',value:String(id)}});
 }
 const name=own.querySelector('.label-name,.user-name,.name')?.textContent?.trim();
 const phone=own.textContent.match(/1\d{2}\*{4}\d{4}/)?.[0];
 return JSON.stringify({browser,identity:name&&phone?{kind:'name_phone',name,phone}:null});
})()"""


def request(config, action, args=None):
    conn = http.client.HTTPConnection(config['host'], config['port'], timeout=20)
    try:
        conn.request('POST', '/command', json.dumps({'action': action, 'args': args or {},
                     'session': config['session']}), {'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = json.loads(response.read().decode())
        if response.status != 200 or data.get('ok') is not True:
            raise RuntimeError(f'WebBridge 未确认 {action}: {data.get("error", "未知错误")}')
        return data.get('data', {})
    finally:
        conn.close()


def inspect_identity(config, prepare=False):
    if prepare:
        # Only this account's session; never borrow another task's active tab.
        request(config, 'navigate', {'url': 'https://www.zhipin.com/web/geek/jobs?ka=header-jobs'})
    for attempt in range(6 if prepare else 1):
        state = json.loads(request(config, 'evaluate', {'code': IDENTITY_JS}).get('value') or '{}')
        if state.get('browser') != config['browser']:
            raise RuntimeError('连接的浏览器与配置不符，停止操作')
        if state.get('identity'):
            return state
        if prepare and attempt < 5:
            time.sleep(1)
    raise RuntimeError('无法可靠读取当前账户身份；请登录，若已登录则需适配身份读取，不允许猜测')


def verify_identity(config=None, prepare=False):
    if config is None:
        from scripts.execution_context import assert_run
        config = assert_run()
    if config is None:
        return None  # Existing single-account behavior, before multi-account activation.
    state = inspect_identity(config, prepare=prepare)
    if not config.get('expected_identity') or state['identity'] != config['expected_identity']:
        raise RuntimeError('当前登录账户与配置绑定身份不符，已停止发送')
    return state


def guarded_script(code):
    """Check again inside the very JS evaluation that may send a message."""
    from scripts.execution_context import assert_run
    config = assert_run()
    if config is None:
        return code
    expected = json.dumps({'browser': config['browser'], 'identity': config['expected_identity']}, ensure_ascii=True)
    return (f'(() => {{ const actual = JSON.parse({IDENTITY_JS}); const expected = {expected}; '
            "if (actual.browser !== expected.browser || !actual.identity || !expected.identity || "
            "Object.keys(actual.identity).length !== Object.keys(expected.identity).length || "
            "!Object.keys(expected.identity).every(k => actual.identity[k] === expected.identity[k])) "
            "throw new Error('BOSS_ACCOUNT_MISMATCH'); "
            f'return (0,eval)({json.dumps(code, ensure_ascii=True)}); }})()')


def bridge_status(config):
    conn = http.client.HTTPConnection(config['host'], config['port'], timeout=3)
    try:
        conn.request('GET', '/status')
        response = conn.getresponse()
        result = json.loads(response.read().decode())
        if response.status != 200 or not result.get('running') or result.get('port') != config['port']:
            raise RuntimeError('目标端口不是预期的 WebBridge 服务')
        return result
    finally:
        conn.close()
