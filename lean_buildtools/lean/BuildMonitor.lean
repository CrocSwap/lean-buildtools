import Lean.Elab.Command
import Lean.Data.Json.FromToJson

open Lean Elab Command

namespace BuildTools

initialize busy : IO.Ref Bool ← IO.mkRef false

@[extern "lean_buildtools_rss"] opaque currentRSS : IO UInt64

private def emit (event : Json) : IO Unit := do
  if let some path ← IO.getEnv "LEAN_BUILDTOOLS_EVENTS" then
    let stream ← IO.FS.Handle.mk path .append
    stream.putStrLn event.compress
    stream.flush

private def stamp (phase : String) (stx : Syntax) : CommandElabM Unit := do
  let ctx ← read
  let pos := ctx.fileMap.toPosition (stx.getPos?.getD ctx.cmdPos)
  liftIO <| emit <| Json.mkObj [
    ("event", toJson phase), ("module", toJson (← getEnv).mainModule.toString),
    ("file", toJson ctx.fileName), ("line", toJson pos.line),
    ("byte_start", toJson (stx.getPos?.getD ctx.cmdPos).byteIdx),
    ("byte_end", toJson (stx.getTailPos?.getD ctx.cmdPos).byteIdx),
    ("syntax", toJson stx.getKind.toString),
    ("namespace", toJson (← getCurrNamespace).toString),
    ("pid", toJson (← IO.Process.getPID).toNat),
    ("rss_bytes", toJson (← currentRSS).toNat),
    ("time_ns", toJson (← IO.monoNanosNow)),
    ("heartbeats", toJson (← IO.getNumHeartbeats))]

-- A second dispatch with a recursion guard preserves Lean's ordinary elaborators.
-- This runs in the compiler, rather than in a separate approximation of a build.
private def synchronousDispatch (stx : Syntax) : CommandElabM Unit := do
  let saved ← get
  let entries := commandElabAttribute.getEntries (← getEnv) stx.getKind
    |>.filter (·.declName != `BuildTools.monitor)
  let rec dispatch : List (KeyedDeclsAttribute.AttributeEntry CommandElab) → CommandElabM Unit
    | [] => throwError "no ordinary elaborator for {stx.getKind}"
    | entry :: rest => catchInternalId unsupportedSyntaxExceptionId
        (entry.value stx) (fun _ => do set saved; dispatch rest)
  withScope (fun scope => { scope with opts := scope.opts.setBool `Elab.async false }) (dispatch entries)

def monitor : CommandElab := fun stx => do
  if ← busy.get then
    if #[`Lean.Parser.Command.declaration, `Lean.Parser.Command.example,
          `Lean.Parser.Command.mutual].contains stx.getKind then
      return ← synchronousDispatch stx
    else
      throwUnsupportedSyntax
  stamp "start" stx
  busy.set true
  try
    elabCommand stx
  finally
    busy.set false
    stamp "end" stx

builtin_initialize
  let keys := (← commandElabAttribute.tableRef.get).fold (init := #[]) fun keys key _ => keys.push key
  for key in keys do
    commandElabAttribute.addBuiltin key `BuildTools.monitor monitor
  -- Leave enough startup time for the parent to inspect the actual exec argv.
  if (← IO.getEnv "LEAN_BUILDTOOLS_EVENTS").isSome then IO.sleep 80

end BuildTools
