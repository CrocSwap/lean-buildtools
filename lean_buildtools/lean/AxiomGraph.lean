import Lean.Elab.Command
import Lean.Util.CollectAxioms
import Lean.Util.FoldConsts
import Lean.Data.Json.FromToJson

/-! Read complete declaration data without constructing a global import environment.
This is metadata tooling, not an independent proof checker. Lean 4.35 is required.
-/

open Lean Elab Command

namespace BuildTools.AxiomGraph

private def copyString (value : String) : String := String.ofList value.toList

private def copyName : Name → Name
  | .anonymous => .anonymous
  | .str parent text => .str (copyName parent) (copyString text)
  | .num parent number => .num (copyName parent) number.repr.toNat!

private def kind : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .thmInfo _ => "theorem"
  | .defnInfo _ => "definition"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"
  | .quotInfo _ => "quotient"

private def dependencies (info : ConstantInfo) : Array Name := Id.run do
  if info matches .quotInfo _ then return #[]
  let mut names := info.type.getUsedConstantsAsSet
  if let some body := info.value? (allowOpaque := true) then
    names := names ++ body.getUsedConstantsAsSet
  match info with
  | .inductInfo value =>
    for name in value.ctors do names := names.insert name
  | _ => pure ()
  return names.toArray.qsort Name.lt

private def emit (stream : IO.FS.Handle) (value : Json) : IO Unit :=
  stream.putStrLn value.compress

/-- The return value contains only copied names and unboxed region handles.
All references into mapped declaration data die before the caller frees regions. -/
private unsafe def dumpModule (module : Name) (stream : IO.FS.Handle) (metadataOnly : Bool)
    (artifacts : NameMap System.FilePath) :
    IO (Array Name × Array CompactedRegion) := do
  let path ← match artifacts.find? module with
    | some path => pure path
    | none => findOLean module
  let currentDir ← IO.currentDir
  let path := if path.isAbsolute then path else currentDir / path
  let (publicData, publicRegion) ← readModuleData path
  let isModule := publicData.isModule
  let paths := if isModule then
      #[path, OLeanLevel.server.adjustFileName path, OLeanLevel.private.adjustFileName path]
    else #[path]
  -- The same ordered dependent-region loading used by readModuleDataParts,
  -- retaining the first part read above to avoid mapping it twice.
  let mut parts := #[(publicData, publicRegion)]
  if isModule then
    let server ← CompactedRegion.read (α := ModuleData) paths[1]! #[publicRegion]
    let priv ← CompactedRegion.read (α := ModuleData) paths[2]! #[publicRegion, server.2]
    parts := parts.push server |>.push priv
  let some (data, _) := parts.back? | throw <| IO.userError "empty module data"
  unless data.constNames.size == data.constants.size do
    throw <| IO.userError s!"malformed constant index: {module}"
  let imports := data.imports.map (fun item => copyName item.module)
  let mut summaries : NameMap (Array Name) := {}
  let mut summaryEntryFound := false
  for (name, entries) in data.entries do
    if name.toString == "_private.Lean.Util.CollectAxioms.0.Lean.exportedAxiomsExt" then
      summaryEntryFound := true
      -- The extension fixes this serialized entry type in the pinned SDK.
      let entries : Array (Name × Array Name) := unsafeCast entries
      for (name, axioms) in entries do summaries := summaries.insert name axioms
  emit stream <| Json.mkObj [
    ("event", toJson "module"), ("module", toJson module.toString),
    ("path", toJson path.toString), ("is_module", toJson isModule),
    ("parts", toJson <| paths.map System.FilePath.toString),
    ("imports", toJson <| imports.map Name.toString),
    ("summary_extension", toJson summaryEntryFound),
    ("declaration_count", toJson data.constants.size)]
  unless metadataOnly do
   for name in data.constNames, info in data.constants do
    unless name == info.name do
      throw <| IO.userError s!"constant index/name mismatch: {module}/{name}"
    emit stream <| Json.mkObj [
      ("event", toJson "declaration"), ("module", toJson module.toString),
      ("name", toJson info.name.toString), ("kind", toJson (kind info)),
      ("unsafe", toJson info.isUnsafe), ("partial", toJson info.isPartial),
      ("dependencies", toJson <| (dependencies info).map Name.toString),
      ("cached_axioms", match summaries.find? info.name with
        | some axioms => toJson (axioms.map Name.toString)
        | none => Json.null)]
  stream.flush
  return (imports, parts.map (·.2))

unsafe def run (requestPath : String) : IO Unit := do
  let request ← IO.ofExcept <| Json.parse (← IO.FS.readFile requestPath)
  let modules ← IO.ofExcept <| request.getObjValAs? (Array String) "modules"
  let output ← IO.ofExcept <| request.getObjValAs? String "output"
  let metadataOnly ← IO.ofExcept <| request.getObjValAs? Bool "metadata_only"
  let entries ← IO.ofExcept <| request.getObjValAs? (Array (String × String)) "artifacts"
  let artifacts : NameMap System.FilePath := entries.foldl
    (fun result (name, path) => result.insert name.toName path) {}
  let stream ← IO.FS.Handle.mk output .write
  let mut pending := modules.map String.toName
  let mut seen : NameHashSet := {}
  while !pending.isEmpty do
    let module := pending.back!
    pending := pending.pop
    if seen.contains module then continue
    seen := seen.insert module
    let (imports, regions) ← dumpModule module stream metadataOnly artifacts
    regions.forM CompactedRegion.free
    pending := pending ++ imports
  emit stream <| Json.mkObj [("event", toJson "complete"), ("modules", toJson seen.size)]
  stream.flush

end BuildTools.AxiomGraph
