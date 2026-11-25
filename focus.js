ObjC.import('AppKit'); ObjC.import('CoreGraphics');
function run(argv) {
  const pid = parseInt(argv[0], 10), mode = argv[1] || 'focus';
  const info = ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionAll | $.kCGWindowListExcludeDesktopElements, 0));
  const wins = [];
  for (let i = 0; i < info.count; i++) {
    const w = info.objectAtIndex(i);
    if (w.objectForKey('kCGWindowOwnerPID').intValue !== pid || w.objectForKey('kCGWindowLayer').intValue !== 0) continue;
    const b = w.objectForKey('kCGWindowBounds');
    const wd = b.objectForKey('Width').intValue, ht = b.objectForKey('Height').intValue;
    if (wd < 200 || ht < 150) continue;
    const on = w.objectForKey('kCGWindowIsOnscreen'); wins.push({on: on ? on.boolValue : false, wd, ht});
  }
  if (mode === 'list') return JSON.stringify(wins);
  if (!wins.length) return 'none';
  const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(pid);
  if (!app) return 'none';
  app.unhide;
  if (!wins.some(w => w.on)) {   // 全部最小化：给这个进程发"重新打开"，Chrome 会还原最小化的窗口
    const t = $.NSAppleEventDescriptor.descriptorWithProcessIdentifier(pid);
    const ev = $.NSAppleEventDescriptor.appleEventWithEventClassEventIDTargetDescriptorReturnIDTransactionID(0x61657674, 0x72617070, t, -1, 0);
    ev.sendEventWithOptionsTimeoutError(0x3 /* kAENoReply */, 2, null);
  }
  const ok = app.activateWithOptions($.NSApplicationActivateAllWindows | (1 << 1));
  return (wins.some(w => w.on) ? 'shown' : 'restored') + ' activate=' + ok;
}
