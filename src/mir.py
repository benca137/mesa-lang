"""
Mesa MIR data model and text printer.

This is intentionally standalone and is not wired into the main compiler
pipeline yet. The goal is to give us a readable SSA-like IR surface we can
iterate on before replacing existing backends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


INDENT = "    "


@dataclass
class MIRTypeDecl:
    name: str
    body: str


@dataclass
class MIRParam:
    name: str
    type_: Optional[str] = None

    def render(self) -> str:
        if self.type_:
            return f"{self.name}: {self.type_}"
        return self.name


@dataclass
class MIRInstruction:
    def render(self) -> str:
        raise NotImplementedError


@dataclass
class MIRAssign(MIRInstruction):
    target: str
    value: str
    result_type: Optional[str] = None
    annotate_type: bool = False

    def render(self) -> str:
        line = f"{self.target} = {self.value}"
        if self.annotate_type and self.result_type:
            line += f" -> {self.result_type}"
        return line


@dataclass
class MIREval(MIRInstruction):
    value: str

    def render(self) -> str:
        return self.value


@dataclass
class MIRTerminator:
    def render(self) -> str:
        raise NotImplementedError


@dataclass
class MIRSwitchCase:
    label: str
    target: str

    def render(self) -> str:
        return f"{self.label} -> {self.target}"


@dataclass
class MIRGoto(MIRTerminator):
    target: str
    args: List[str] = field(default_factory=list)

    def render(self) -> str:
        if self.args:
            return f"goto {self.target}({', '.join(self.args)})"
        return f"goto {self.target}"


@dataclass
class MIRCmpGoto(MIRTerminator):
    condition: str
    then_target: str
    else_target: str

    def render(self) -> str:
        return f"if {self.condition} goto {self.then_target} else {self.else_target}"


@dataclass
class MIRSwitchTag(MIRTerminator):
    value: str
    cases: List[MIRSwitchCase] = field(default_factory=list)
    default_target: Optional[str] = None

    def render(self) -> str:
        lines = [f"switch.tag {self.value}:"]
        for case in self.cases:
            lines.append(f"{INDENT}{case.render()}")
        if self.default_target is not None:
            lines.append(f"{INDENT}_ -> {self.default_target}")
        return "\n".join(lines)


@dataclass
class MIRSwitchResult(MIRTerminator):
    value: str
    ok_target: str
    err_target: str

    def render(self) -> str:
        lines = [f"switch.result {self.value}:"]
        lines.append(f"{INDENT}ok -> {self.ok_target}")
        lines.append(f"{INDENT}err -> {self.err_target}")
        return "\n".join(lines)


@dataclass
class MIRReturn(MIRTerminator):
    value: Optional[str] = None

    def render(self) -> str:
        if self.value is None:
            return "return"
        return f"return {self.value}"


@dataclass
class MIRBlock:
    name: str
    params: List[MIRParam] = field(default_factory=list)
    instructions: List[MIRInstruction] = field(default_factory=list)
    terminator: Optional[MIRTerminator] = None

    def header(self) -> str:
        if self.params:
            return f"{self.name}({', '.join(param.render() for param in self.params)}):"
        return f"{self.name}:"

    def render(self) -> str:
        lines = [self.header()]
        for inst in self.instructions:
            for rendered in inst.render().splitlines():
                lines.append(f"{INDENT}{rendered}")
        if self.terminator is not None:
            for rendered in self.terminator.render().splitlines():
                lines.append(f"{INDENT}{rendered}")
        return "\n".join(lines)


@dataclass
class MIRFunction:
    name: str
    params: List[MIRParam]
    return_type: str
    blocks: List[MIRBlock] = field(default_factory=list)
    is_extern: bool = False

    def render(self) -> str:
        params = ", ".join(param.render() for param in self.params)
        if self.is_extern:
            return f"extern @{self.name}({params}) {self.return_type}"
        lines = [f"@{self.name}({params}) {self.return_type}:"]
        for idx, block in enumerate(self.blocks):
            if idx:
                lines.append("")
            lines.append(block.render())
        return "\n".join(lines)


@dataclass
class MIRModule:
    type_decls: List[MIRTypeDecl] = field(default_factory=list)
    functions: List[MIRFunction] = field(default_factory=list)

    def render(self) -> str:
        sections: List[str] = []
        if self.type_decls:
            sections.extend(td.render() if hasattr(td, "render") else f"type {td.name} = {td.body}" for td in self.type_decls)
        if self.functions:
            if sections:
                sections.append("")
            sections.extend(fn.render() for fn in self.functions)
        return "\n\n".join(sections).rstrip() + "\n"
