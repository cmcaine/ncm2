# -*- coding: utf-8 -*-

import re
import vim
from ncm2 import Ncm2Base, getLogger
import json
import glob
from os import path, environ
from importlib import import_module
from copy import deepcopy
import time

# don't import this module by other processes
assert environ['NVIM_YARP_MODULE'] == 'ncm2_core'

logger = getLogger(__name__)


class Ncm2Core(Ncm2Base):

    def __init__(self, nvim):

        super().__init__(nvim)

        # { '{source_name}': {'startccol': , 'matches'}
        self._cache_lnum = 0
        self._matches = {}
        self._last_popup = []
        self._notified = {}
        self._subscope_detectors = {}

        self._loaded_plugins = {}

        pats = {}
        pats['*'] = r'(-?\d*\.\d\w*)|([^\`\~\!\@\#\$\%\^\&\*\(\)\-\=\+\[\{\]\}\\\|\;\:\'\"\,\.\<\>\/\?\s]+)'
        pats['css'] = r'(-?\d*\.\d[\w-]*)|([^\`\~\!\@\#\$\%\^\&\*\(\)\=\+\[\{\]\}\\\|\;\:\'\"\,\.\<\>\/\?\s]+)'
        pats['scss'] = pats['css']
        pats['php'] = r'(-?\d*\.\d\w*)|([^\-\`\~\!\@\#\%\^\&\*\(\)\=\+\[\{\]\}\\\|\;\:\'\"\,\.\<\>\/\?\s]+)'
        pats['vim'] = r'(-?\d*\.\d\w*)|([^\-\`\~\!\@\%\^\&\*\(\)\=\+\[\{\]\}\\\|\;\'\"\,\.\<\>\/\?\s]+)'

        self._word_patterns = pats

    def notify(self, method: str, *args):
        self.nvim.call(method, *args, async_=True)

    def get_word_pattern(self, ctx, sr):
        pat = sr.get('word_pattern', None)
        scope = ctx.get('scope', ctx.get('filetype', '')).lower()

        if type(pat) == dict:
            pat = pat.get(scope, pat.get('*', None))

        if type(pat) == str:
            return pat

        pats = self._word_patterns
        return pats.get(scope, pats['*'])

    def load_plugin(self, _, rtp: str):
        self.update_rtp(rtp)

        for d in rtp.split(','):
            for vs in glob.iglob(path.join(d, 'ncm2-plugin/*.vim')):
                if vs in self._loaded_plugins:
                    continue
                self._loaded_plugins[vs] = True
                logger.info('send vimscript plugin %s', vs)
                self.notify('ncm2#_load_vimscript', vs)

            for py in glob.iglob(path.join(d, 'ncm2-plugin/*.py')):
                if py in self._loaded_plugins:
                    continue
                self._loaded_plugins[py] = True
                logger.info('send python plugin %s', py)
                # async_call to get multiple exceptions properly printed
                self.nvim.async_call(lambda: self.load_python({}, py))

            dts = glob.glob(path.join(d, 'pythonx/ncm2_subscope_detector/*.py')) + \
                glob.glob(path.join(d, 'python3/ncm2_subscope_detector/*.py'))
            self.load_subscope_detectors(dts)

        self.notify('ncm2#_au_plugin')

    def load_python(self, _, py):
        with open(py, "rb") as f:
            src = f.read()
            exec(compile(src, py, 'exec'), {}, {})

    def load_subscope_detectors(self, paths):
        new_scope = False

        # auto find scopers
        for py in paths:
            if py in self._loaded_plugins:
                continue
            self._loaded_plugins[py] = True

            try:
                mod = path.splitext(path.basename(py))[0]
                mod = "ncm2_subscope_detector.%s" % mod
                m = import_module(mod)
            except Exception as ex:
                logger.exception('importing scoper <%s> failed', py)
                continue

            sd = m.SubscopeDetector(self.nvim)

            for scope in sd.scope:
                if scope not in self._subscope_detectors:
                    self._subscope_detectors[scope] = []
                    new_scope = True

                self._subscope_detectors[scope].append(sd)

            logger.info('subscope detector <%s> for %s', py, sd.scope)

        if not new_scope:
            return

        detectors_sync = {}
        for scope in self._subscope_detectors.keys():
            detectors_sync[scope] = 1

        self.notify('ncm2#_s', 'subscope_detectors', detectors_sync)

    def get_context(self, data, name):
        if type(name) is str:
            sr = data['sources'][name]
        else:
            sr = name
            name = sr['name']
        root_ctx = data['context']
        contexts = self.detect_subscopes(data)
        for ctx in contexts:
            ctx = deepcopy(ctx)
            ctx['source'] = sr
            ctx['matcher'] = self.matcher_opt_get(data, sr)
            if not self.source_check_scope(sr, ctx):
                continue
            self.source_check_patterns(data, sr, ctx)
            ctx['time'] = time.time()
            return ctx

    def on_complete_done(self, data, completed):
        logger.info('on_complete_done')

        # is completed item from ncm2
        try:
            completed['user_data'] = json.loads(completed['user_data'])
            ud = completed['user_data']
            if not ud.get('ncm2', 0):
                logger.debug(
                    'This is not completed by ncm2, ncm2==0, ud: %s', ud)
                return
        except Exception as ex:
            logger.debug('This is not completed by ncm2, %s', ex)
            return

        name = ud['source']

        sr = data['sources'].get(name, None)
        if not sr:
            logger.error('the source does not exist')
            return

        if not sr.get('on_completed', None):
            logger.debug(
                'the source does not have on_completed handler, %s', sr)
            return

        root_ctx = data['context']
        root_ctx['manual'] = 0

        # regenerate contexts for this source
        contexts = self.detect_subscopes(data)
        for ctx in contexts:
            ctx = deepcopy(ctx)
            ctx['source'] = sr
            ctx['matcher'] = self.matcher_opt_get(data, sr)
            if not self.source_check_scope(sr, ctx):
                continue
            self.source_check_patterns(data, sr, ctx)
            self._notified[name] = ctx
            ctx['time'] = time.time()
            ctx['event'] = 'on_completed'
            self.notify('ncm2#_notify_completed',
                        root_ctx,
                        name,
                        ctx,
                        completed)
            return

    def on_notify_dated(self, data, _, failed_notifies=[]):
        for ele in failed_notifies:
            name = ele['name']
            ctx = ele['context']
            notified = self._notified
            if name in notified and notified[name] == ctx:
                logger.debug('%s notification is dated', name)
                del notified[name]

    def on_complete(self, data, manual, failed_notifies=[]):

        root_ctx = data['context']
        root_ctx['manual'] = manual

        self.cache_cleanup_check(root_ctx)

        contexts = self.detect_subscopes(data)

        # do notify_sources_to_refresh
        notifies = []

        # get the sources that need to be notified
        for tmp_ctx in contexts:
            for name, sr in data['sources'].items():

                ctx = deepcopy(tmp_ctx)
                ctx['early_cache'] = False
                ctx['source'] = sr
                ctx['matcher'] = self.matcher_opt_get(data, sr)

                if not self.check_source_notify(data, sr, ctx):
                    continue
                self._notified[name] = ctx
                notifies.append(dict(name=name, context=ctx))

        if notifies:
            cur_time = time.time()
            for noti in notifies:
                ctx = noti['context']
                ctx['time'] = cur_time
            self.notify('ncm2#_notify_complete', root_ctx, notifies)
        else:
            logger.debug('notifies is empty %s', notifies)

        self.matches_update_popup(data)

    def on_warmup(self, data, names):
        warmups = []

        if not names:
            names = list(data['sources'].keys())

        for ctx_idx, tmp_ctx in enumerate(self.detect_subscopes(data)):
            for name in names:
                sr = data['sources'][name]

                ctx = deepcopy(tmp_ctx)
                ctx['early_cache'] = False
                ctx['source'] = sr

                if not sr['enable']:
                    continue

                if not self.source_check_scope(sr, ctx):
                    continue

                warmups.append(dict(name=name, context=ctx))

        self.notify('ncm2#_warmup_sources', data['context'], warmups)

    def check_source_notify(self, data, sr, ctx):
        name = sr['name']

        cache = self._matches.get(name, None)

        if not sr['enable']:
            logger.debug('%s is not enabled', name)
            return False

        if not sr['ready']:
            logger.debug('%s is not ready', name)
            return False

        if not self.source_check_scope(sr, ctx):
            logger.debug(
                'source_check_scope ignore <%s> for context scope <%s>', name, ctx['scope'])
            return False

        manual = ctx.get('manual', 0)

        if not sr['auto_popup'] and not manual:
            logger.debug('<%s> is not auto_popup', name)
            return False

        # check patterns
        if not self.source_check_patterns(data, sr, ctx):
            if sr['early_cache'] and len(ctx['base']):
                ctx['early_cache'] = True
            else:
                logger.debug(
                    'source_check_patterns failed ignore <%s> base %s', name, ctx['base'])
                if cache:
                    cache['enable'] = False
                return False
        else:
            # enable cached
            if cache:
                logger.debug('<%s> enable cache', name)
                cache['enable'] = True

        need_refresh = False

        if not manual:

            # if there's valid cache
            if cache:
                need_refresh = cache['refresh']
                cc = cache['context']
                if not need_refresh and self.is_kw_type(data, sr, cc, ctx):
                    logger.debug('<%s> was cached, context: %s matches: %s',
                                 name, cc, cache['matches'])
                    return False

            # we only notify once for each word
            noti = self._notified.get(name, None)
            if noti and not need_refresh:
                if self.is_kw_type(data, sr, noti, ctx):
                    logger.debug(
                        '<%s> has been notified, cache %s', name, cache)
                    return False

        if need_refresh:
            # reduce further duplicate notification
            cache['refresh'] = 0
        return True

    def complete(self, data, sctx, startccol, matches, refresh):
        ctx = data['context']
        self.cache_cleanup_check(ctx)

        name = sctx['source']['name']

        sr = data['sources'].get(name, None)
        if not sr:
            logger.error("%s] source does not exist", name)
            return

        cache = self._matches.get(name, None)
        if cache and cache['context']['context_id'] > sctx['context_id']:
            logger.debug('%s cache is newer, %s', name, cache)
            return

        dated = sctx['dated']

        # be careful when completion matches context is dated
        if dated:
            if not self.is_kw_type(data, sr, sctx, ctx):
                logger.info("[%s] dated is_kw_type fail, old[%s] cur[%s]",
                            name, sctx['typed'], ctx['typed'])
                return
            else:
                logger.info("[%s] dated is_kw_type ok, old[%s] cur[%s]",
                            name, sctx['typed'], ctx['typed'])

        # adjust for subscope
        if sctx['lnum'] == 1:
            startccol += sctx.get('scope_ccol', 1) - 1

        matches = self.matches_formalize(sctx, matches)

        # filter before cache
        old_le = len(matches)
        matches = self.matches_filter_by_matcher(
            data, sr, sctx, startccol, matches)
        logger.debug('%s matches is filtered %s -> %s',
                     name, old_le, len(matches))

        if not cache:
            self._matches[name] = {}
            cache = self._matches[name]

        cache['startccol'] = startccol
        cache['refresh'] = refresh
        cache['matches'] = matches
        cache['context'] = sctx
        cache['enable'] = not sctx.get('early_cache', False)

        self.matches_update_popup(data)

    def is_kw_type(self, data, sr, ctx1, ctx2):
        ctx1 = deepcopy(ctx1)
        ctx2 = deepcopy(ctx2)

        self.source_check_patterns(data, sr, ctx1)
        self.source_check_patterns(data, sr, ctx2)

        logger.debug('old ctx [%s] cur ctx [%s]', ctx1, ctx2)
        # startccol is set in self.source_check_patterns
        c1s, c1e, c1b = ctx1['startccol'], ctx1['match_end'], ctx1['base']
        c2s, c2e, c2b = ctx2['startccol'], ctx1['match_end'], ctx2['base']
        return c1s == c2s and c1b == c2b[:len(c1b)]

    # InsertEnter, InsertLeave, or lnum changed
    def cache_cleanup(self, *args):
        self._matches = {}
        self._notified = {}
        self._last_popup = []

    def cache_cleanup_check(self, ctx):
        if self._cache_lnum != ctx['lnum']:
            self.cache_cleanup()
            self._cache_lnum = ctx['lnum']

    def detect_subscopes(self, data):
        root_ctx = data['context']
        root_ctx['scope_level'] = 1
        ctx_list = [root_ctx]
        sync_detectors = data['subscope_detectors']
        src = '\n'.join(data['lines'])

        i = 0
        while i < len(ctx_list):
            ctx = ctx_list[i]
            i += 1
            scope = ctx['scope']

            if not sync_detectors.get(scope, False):
                continue

            if not self._subscope_detectors.get(scope, None):
                continue

            for sd in self._subscope_detectors[scope]:
                try:
                    lnum, ccol = ctx['lnum'], ctx['ccol']
                    scope_src = self.get_src(src, ctx)

                    res = sd.detect(lnum, ccol, scope_src)
                    if not res:
                        continue
                    sub = deepcopy(ctx)
                    sub.update(res)

                    # adjust offset to global based and add the new context
                    sub['scope_offset'] += ctx.get('scope_offset', 0)
                    sub['scope_lnum'] += ctx.get('scope_lnum', 1) - 1
                    sub['scope_level'] += 1

                    if sub['lnum'] == 1:
                        sub['typed'] = sub['typed'][sub['scope_ccol'] - 1:]
                        sub['scope_ccol'] += ctx.get('scope_ccol', 1) - 1

                    ctx_list.append(sub)
                    logger.info('new sub context: %s', sub)
                except Exception as ex:
                    logger.exception(
                        "exception on scope processing: %s", ex)

        return ctx_list

    def source_check_patterns(self, data, sr, ctx):
        pats = sr.get('complete_pattern', [])
        if type(pats) == str:
            pats = [pats]

        typed = ctx['typed']
        word_pat = self.get_word_pattern(ctx, sr)

        # remove the last word, check whether the special pattern matches
        # word_removed
        end_word_matched = re.search(word_pat + "$", typed)
        if end_word_matched:
            ctx['base'] = end_word_matched.group()
            ctx['startccol'] = ctx['ccol'] - len(ctx['base'])
            word_removed = typed[:end_word_matched.start()]
            word_len = len(ctx['base'])
        else:
            ctx['base'] = ''
            ctx['startccol'] = ctx['ccol']
            word_removed = typed
            word_len = 0

        ctx['match_end'] = len(word_removed)
        ctx['word_pattern'] = word_pat

        # check source extra patterns
        for pat in pats:
            # use greedy match '.*', to push the match to the last occurance
            # pattern
            if not pat.startswith("^"):
                pat = '.*' + pat

            matched = re.search(pat, typed)
            if matched and matched.end() >= len(typed) - word_len:
                ctx['match_end'] = matched.end()
                return True

        cmplen = self.source_get_complete_len(data, sr)
        if cmplen is None:
            return False

        if cmplen < 0:
            return False

        return word_len >= cmplen

    def source_get_complete_len(self, data, sr):
        if 'complete_length' in sr:
            return sr['complete_length']

        cmplen = data['complete_length']
        if type(cmplen) == int:
            return cmplen

        pri = sr['priority']

        # format: [ [ minimal priority, min length ], []]
        val = None
        mxpri = -1
        for e in cmplen:
            if pri >= e[0] and e[0] > mxpri:
                val = e[1]
                mxpri = e[0]
        return val

    def source_check_scope(self, sr, ctx):
        scope = sr.get('scope', None)
        cur_scope = ctx['scope']
        ctx['scope_match'] = ''
        is_root = ctx['scope_level'] == 1
        if not scope:
            # scope setting is None, means that this is a general purpose
            # completion source, only complete for the root scope
            if is_root:
                return True
            else:
                return False

        for scope in scope:
            if scope == cur_scope:
                ctx['scope_match'] = scope
                if sr['subscope_enable']:
                    return True
                else:
                    return is_root
        return False

    def matches_update_popup(self, data):
        ctx = data['context']

        # sort by priority
        names = self._matches.keys()
        srcs = data['sources']
        names = sorted(names, key=lambda x: srcs[x]['priority'], reverse=True)

        ccol = ctx['ccol']

        # basic filtering for matches of each source
        names_with_matches = []
        for name in names:

            sr = srcs.get(name, None)
            if not sr:
                logger.error('[%s] source does not exist', name)
                continue

            cache = self._matches[name]
            cache['filtered_matches'] = []

            if not cache['enable']:
                logger.debug('<%s> is disabled', name)
                continue

            sccol = cache['startccol']
            if sccol > ccol or sccol == 0:
                logger.warn('%s invalid startccol %s', name, sccol)
                continue

            smat = deepcopy(cache['matches'])
            sctx = cache['context']

            if data['skip_tick']:
                if sctx.get('event', '') != 'on_completed' or \
                        sctx['tick'] != data['skip_tick']:
                    logger.debug('%s matches ignored by skip_tick',
                                 data['skip_tick'])
                    continue

            smat = self.matches_filter(data, sr, sctx, sccol, smat)
            cache['filtered_matches'] = smat

            logger.debug('%s matches is filtered %s -> %s',
                         name, len(cache['matches']), len(smat))

            if not smat:
                continue

            names_with_matches.append(name)

        # additional filtering on inter-source level
        names = self.get_sources_for_popup(data, names_with_matches)

        # merge results of sources, popup_limit
        startccol = ccol
        for name in names:
            sr = srcs[name]
            cache = self._matches[name]
            sccol = cache['startccol']
            filtered_matches = cache['filtered_matches']

            # popup_limit
            popup_limit = sr.get('popup_limit', data['popup_limit'])
            if popup_limit >= 0:
                filtered_matches = filtered_matches[: popup_limit]
                if len(filtered_matches) != len(cache['filtered_matches']):
                    logger.debug('%s matches popup_limit %s -> %s',
                                 name,
                                 len(cache['filtered_matches']),
                                 len(filtered_matches))
                    cache['filtered_matches'] = filtered_matches

            for m in filtered_matches:
                ud = m['user_data']
                mccol = ud.get('startccol', sccol)
                if mccol < startccol:
                    startccol = mccol

        typed = ctx['typed']
        matches = []
        for name in names:

            try:
                sr = srcs[name]
                cache = self._matches[name]
                smat = cache['filtered_matches']
                if not smat:
                    continue

                sccol = cache['startccol']
                for e in smat:
                    ud = e['user_data']
                    mccol = ud.get('startccol', sccol)
                    prefix = typed[startccol-1: mccol-1]
                    dw = self.strdisplaywidth(prefix)
                    space_pad = ' ' * dw

                    e['abbr'] = space_pad + e['abbr']
                    e['word'] = prefix + e['word']
                matches += smat

            except Exception as inst:
                logger.exception(
                    '_refresh_completions process exception: %s', inst)
                continue

        logger.info('popup names: %s, startccol: %s, matches cnt: %s',
                    names, startccol, len(matches))

        matches = self.matches_decorate(data, matches)

        self.matches_do_popup(ctx, startccol, matches)

    def get_sources_for_popup(self, data, names):
        return names

    def matcher_opt_get(self, data, sr):
        gmopt = self.matcher_opt_formalize(data['matcher'])
        smopt = {}
        if 'matcher' in sr:
            smopt = self.matcher_opt_formalize(sr['matcher'])
        gmopt.update(smopt)
        return gmopt

    def sorter_opt_formalize(self, opt):
        if type(opt) is str:
            return dict(name=opt)
        return deepcopy(opt)

    def sorter_opt_get(self, data, sr):
        gsopt = self.sorter_opt_formalize(data['sorter'])
        ssopt = {}
        if 'sorter' in sr:
            ssopt = self.sorter_opt_formalize(opt)
        gsopt.update(ssopt)
        return gsopt

    def sorter_get(self, opt):
        name = opt['name']
        modname = 'ncm2_sorter.' + name
        mod = import_module(modname)
        m = mod.Sorter(**opt)
        return m

    def filter_opt_formalize(self, opts):
        opts = deepcopy(opts)
        if type(opts) is not list:
            opts = [opts]
        ret = []
        for opt in opts:
            if type(opt) is str:
                opt = dict(name=opt)
            ret.append(opt)
        return ret

    def filter_opt_get(self, data, sr):
        opt = sr.get('filter', data['filter'])
        return self.filter_opt_formalize(opt)

    def filter_get(self, opts):
        filts = []
        for opt in opts:
            name = opt['name']
            modname = 'ncm2_filter.' + name
            mod = import_module(modname)
            f = mod.Filter(**opt)
            filts.append(f)

        def handler(data, sr, sctx, sccol, matches):
            for f in filts:
                matches = f(data, sr, sctx, sccol, matches)
            return matches
        return handler

    def matches_filter_by_matcher(self, data, sr, sctx, sccol, matches):
        ctx = data['context']
        typed = ctx['typed']
        matcher = self.matcher_get(sctx['matcher'])
        tmp = []
        for m in matches:
            ud = m['user_data']
            mccol = ud.get('startccol', sccol)
            base = typed[mccol-1:]
            if matcher(base, m):
                tmp.append(m)
        return tmp

    def matches_filter(self, data, sr, sctx, sccol, matches):
        matches = self.matches_filter_by_matcher(
            data, sr, sctx, sccol, matches)

        sorter = self.sorter_get(self.sorter_opt_get(data, sr))
        matches = sorter(matches)

        opt = self.filter_opt_get(data, sr)
        filt = self.filter_get(opt)
        matches = filt(data, sr, sctx, sccol, matches)

        return matches

    def matches_decorate(self, data, matches):
        return self.matches_add_source_mark(data, matches)
        return matches

    def matches_add_source_mark(self, data, matches):
        for e in matches:
            name = e['user_data']['source']
            sr = data['sources'][name]
            tag = sr.get('mark', '')
            if tag == '':
                continue
            e['menu'] = "[%s] %s" % (tag, e['menu'])
        return matches

    def matches_do_popup(self, ctx, startccol, matches):
        # json_encode user_data
        for m in matches:
            m['user_data'] = json.dumps(m['user_data'])

        popup = [ctx['tick'], startccol, matches]
        if self._last_popup == popup:
            return
        self._last_popup = popup

        # startccol -> startbcol
        typed = ctx['typed']
        startbcol = len(typed[: startccol-1].encode()) + 1

        self.notify('ncm2#_update_matches', ctx, startbcol, matches)


ncm2_core = Ncm2Core(vim)

events = ['on_complete', 'cache_cleanup',
          'complete', 'load_plugin', 'load_python', 'on_warmup', 'ncm2_core']

on_complete = ncm2_core.on_complete
cache_cleanup = ncm2_core.cache_cleanup
complete = ncm2_core.complete
load_plugin = ncm2_core.load_plugin
load_python = ncm2_core.load_python
on_warmup = ncm2_core.on_warmup
on_notify_dated = ncm2_core.on_notify_dated
on_complete_done = ncm2_core.on_complete_done
get_context = ncm2_core.get_context

__all__ = events
