import pdb
import logging
from .ramblr_utils import CAPSTONE_OP_TYPE_MAP, CAPSTONE_REG_MAP, OP_TYPE_MAP, OP_TYPE_IMM, OP_TYPE_REG, OP_TYPE_MEM, OP_TYPE_OTHER
from .ramblr_errors import BinaryError, InstructionError, ReassemblerFailureNotice
from .labels import FunctionLabel
l = logging.getLogger("angr.analyses.reassembler")

class Operand(object):
    def __init__(self, binary, insn_addr, insn_size, capstone_operand, operand_str, mnemonic, syntax=None):

        """
        Constructor.

        :param Reassembler binary: The Binary analysis.
        :param int insn_addr: Address of the instruction.
        :param capstone_operand:
        :param str operand_str: the string representation of this operand
        :param str mnemonic: Mnemonic of the instruction that this operand belongs to.
        :param str syntax: Provide a way to override the default syntax coming from `binary`.
        :return: None
        """

        self.binary = binary
        self.project = binary.project
        self.insn_addr = insn_addr
        self.insn_size = insn_size
        self.operand_str = operand_str
        self.mnemonic = mnemonic
        self.syntax = self.binary.syntax if syntax is None else syntax

        self.type = None

        # Fixed size architectures in capstone don't have .size
        if hasattr(capstone_operand, 'size'):
            self.size = capstone_operand.size
        else:
            if self.binary.project.arch.name in ['PPC32', 'ARMEL', 'MIPS32']: #Is there ARMEL64? If so, is it just called ARMEL too?
                self.size = 32
            elif self.binary.project.arch.name in ['PPC64']:
                self.size = 64
            else:
                # If you're adding a new architecture, be sure to also add to CAPSTONE_OP_TYPE_MAP and CAPSTONE_REG_MAP
                raise RuntimeError("Architecture '{}' has unknown size operands".format(self.binary.project.arch.name))

        # IMM
        self.is_coderef = None
        self.is_dataref = None
        self.label = None
        self.label_offset = 0
        self.label_suffix = 0
        self.label_prefix = 0

        # MEM
        self.base = None
        self.index = None
        self.scale = None
        self.disp = None

        self.disp_is_coderef = None
        self.disp_is_dataref = None
        self.disp_label = None
        self.disp_label_offset = 0

        self._initialize(capstone_operand)

    #
    # Public methods
    #

    def assembly(self):
        if self.type == OP_TYPE_IMM and self.label:

            # No support for a label offset with pre- or postfixed
            assert not(self.label_offset !=0 and (self.label_suffix or self.label_prefix))


            # Architectures like PPC can load reg@h or reg@l to indicate high or low bits of an address
            # If the label_offset is a string, it should be used as a suffix on 'label@'
            if self.label_suffix:
                return "%s%s" % (self.label.operand_str, self.label_offset)

            if self.label_prefix:
                return "%s%s" % (self.label_prefix, self.label.operand_str)

            # Otherwise, it will be an integer +/- of the label
            if self.label_offset > 0:
                return "%s + %d" % (self.label.operand_str, self.label_offset)
            elif self.label_offset < 0:
                return "%s - %d" % (self.label.operand_str, abs(self.label_offset))
            else:
                return self.label.operand_str

        elif self.type == OP_TYPE_MEM:

            disp = ""
            if self.disp:
                if self.disp_label:
                    if self.disp_label_offset > 0:
                        disp = "%s + %d" % (self.disp_label.operand_str, self.disp_label_offset)
                    elif self.disp_label_offset < 0:
                        disp = "%s - %d" % (self.disp_label.operand_str, abs(self.disp_label_offset))
                    else:
                        disp = self.disp_label.operand_str
                else:
                    disp = "%d" % self.disp

            base = ""
            if self.base:
                base = CAPSTONE_REG_MAP[self.project.arch.name][self.base]

            if self.syntax == 'at&t':
                # displacement(base, index, scale)
                base = "%%%s" % base if base else ""

                if "*" in self.operand_str and disp:
                    # absolute memory address
                    disp = "*" + disp

                if self.index:
                    asm = "%s(%s, %%%s, %d)" % (disp, base, CAPSTONE_REG_MAP[self.project.arch.name][self.index],
                                              self.scale
                                              )
                elif self.base:  # not self.index
                    if self.project.arch.name != "ARMEL" or base != "%r15":
                        asm = "%s(%s)" % (disp, base)
                    else:
                        try:
                            disp = hex(int(disp))
                        except ValueError:
                            pass
                        asm = "=%s" % (disp) # For ARMEL pc-relative LDR is LDR r3, r4 and not LDR r3, r4(r15)
                else:
                    asm = disp

                return asm

            else:
                arr_rep = [ ]
                if base:
                    arr_rep.append(base)

                if self.index and self.scale:
                    arr_rep.append('+')
                    arr_rep.append("(%s * %d)" % (CAPSTONE_REG_MAP[self.project.arch.name][self.index], self.scale))

                if disp:
                    if disp.startswith('-'):
                        arr_rep.append('-')
                        arr_rep.append(disp[1:])
                    else:
                        if arr_rep:
                            arr_rep.append('+')
                        arr_rep.append(disp)

                asm = " ".join(arr_rep)

                # we need to specify the size here
                if 'dword' in self.operand_str.lower():
                    asm = 'dword ptr [%s]' % asm
                elif 'word' in self.operand_str.lower():
                    asm = 'word ptr [%s]' % asm
                elif 'byte' in self.operand_str.lower():
                    asm = 'byte ptr [%s]' % asm
                else:
                    raise BinaryError('Unsupported memory operand size for operand "%s"' % self.operand_str)

                return asm

        else:
            # Nothing special
            return None

    #
    # Overridden predefined methods
    #

    def __str__(self):
        """

        :return:
        """

        op_type = OP_TYPE_MAP[self.type]

        ref_type = ""
        if self.is_coderef:
            ref_type = "CODEREF"
        elif self.is_dataref:
            ref_type = "DATAREF"

        if ref_type:
            return "%s <%s>" % (op_type, ref_type)
        else:
            return op_type

    #
    # Properties
    #

    @property
    def is_immediate(self):
        return self.type == OP_TYPE_IMM

    @property
    def symbolized(self):
        return self.label is not None or self.disp_label is not None

    #
    # Private methods
    #

    def _initialize(self, capstone_operand):
        try:
            self.type = CAPSTONE_OP_TYPE_MAP[self.project.arch.name][capstone_operand.type]
        except KeyError:
            l.error("Unsupported operand type: %s %s", self.project.arch.name, capstone_operand.type)
            raise

        if self.binary.project.arch.name == 'PPC32':
            l.warning("Disabling log_relocations for PPC32. Not sure what that really means...")
            self.binary.log_relocations = False # Not currently supported, not sure if it needs to be

        #if self.insn_addr == 0x00b0ec:
        #    pdb.set_trace()

        if self.type == OP_TYPE_IMM:
            # Check if this is a reference to code
            imm = capstone_operand.imm

            self.is_coderef, self.is_dataref, baseaddr = \
                self._imm_to_ptr(imm, self.type, self.mnemonic)

            if self.is_coderef or self.is_dataref:
                self.label = self.binary.symbol_manager.new_label(addr=baseaddr)
                self.label_offset = imm - baseaddr

                sort = 'absolute'
                if self.binary.project.arch.name == "ARMEL":
                    if self.mnemonic.startswith("b"):
                        sort = 'jump'
                elif self.binary.project.arch.name == "X86":
                    if self.mnemonic.startswith("j") or self.mnemonic.startswith('loop'):
                        sort = 'jump'
                    elif self.mnemonic.startswith("call"):
                        sort = 'call'
                if sort == 'absolute':
                    pass
                    #l.info("Assuming {} on arch {} is an absolute reference\tcoderef={}, dataref={}".format(self.mnemonic,
                        #self.binary.project.arch.name, self.is_coderef, self.is_dataref))

                #l.debug("Found {} {:x} (at 0x{:x}): {} to {}".format(self.mnemonic, imm, self.insn_addr, sort, self.label))
                self.binary.register_instruction_reference(self.insn_addr, imm, sort, self.insn_size, self.binary.project.arch.name)

        elif self.type == OP_TYPE_MEM:
            self.base = capstone_operand.mem.base   # If the instruction is ADD R11, SP, #4 and we're processing #4, this will be SP
            self.disp = capstone_operand.mem.disp
            imm = capstone_operand.imm ### I added this

            if self.binary.project.arch.name in ['PPC32', 'PPC64', 'MIPS32']: # fixed index/scale architecture capstone objects won't have these set(?)
                self.index = 0
                self.scale = 1
            else:
                self.index = capstone_operand.mem.index
                self.scale = capstone_operand.mem.scale

            if self.binary.project.arch.name == 'AMD64' and CAPSTONE_REG_MAP['AMD64'][self.base] == 'rip':
                # rip-relative addressing
                self.disp += self.insn_addr + self.insn_size

            #ARMEL can use PC relative addressing (at least for LDR)
            if self.binary.project.arch.name == 'ARMEL':
                #if not (self.mnemonic.startswith(u"b") or self.mnemonic.startswith(u"cbnz") or self.mnemonic.startswith("cbz")):
                #    self.disp &= 0xFFFFFFFE # Clear the lowest bit to word align
                self.disp += self.insn_addr

                # http://www.keil.com/support/man/docs/armasm/armasm_dom1359731173886.htm
                #For B, BL, CBNZ, and CBZ instructions, the value of the PC is the address of the current instruction plus 4 bytes.
                #For all other instructions that use labels, the value of the PC is the address of the current instruction plus 4 bytes,
                #with bit[1] of the result cleared to 0 to make it word-aligned.


                if self.mnemonic.startswith(u'ldr') :
                    self.disp += 8
                    self.disp_is_coderef, self.disp_is_dataref, baseaddr = \
                        self._imm_to_ptr(self.disp, self.type, self.mnemonic, self.binary.project.arch.name)

                    if CAPSTONE_REG_MAP['ARMEL'][self.base] == "r15": # r15 is IP for ARMEL
                        # LDR will load a data reference every time, that can then point to a codereference
                        self.disp_is_coderef = False
                        self.disp_is_dataref = True

                        if not baseaddr:
                            l.warning("0x{:x}\t LDR skip".format(self.insn_addr))
                            #c, d, b = self._imm_to_ptr(self.disp, self.type, self.mnemonic, self.binary.project.arch.name)
                            return

                        # Can we LDR a coderef? I don't think so, because it shoudl be a dataref that then points to a code or dataref
                        #if self.disp_is_coderef or self.disp_is_dataref:
                        if baseaddr in self.binary.kb.functions:
                            l.warning("\tDelete from functions v2")
                            del self.binary.kb.functions[baseaddr]
                            self.is_dataref = True
                            self.is_coderef = False

                        # Ensure that we'll define label_x as dereferenced_val (ptr or imm)
                        # Try to dereference it, if we can, LDR to the dereferenced address
                        dereferenced_val = self.binary.fast_memory_load(baseaddr, 4, int)
                        if dereferenced_val:
                            new_lbl = self.binary.symbol_manager.new_label(addr=dereferenced_val)
                            print("0x{:x}:\tldr off_{:x} \t = {:<9} =>  {:<15} == 0x{:x}".format(self.insn_addr, baseaddr, "_", new_lbl.name, dereferenced_val))

                            self.disp_label = new_lbl
                            #self.binary.register_instruction_reference(self.disp, dereferenced_val, 'absolute', 4, self.binary.project.arch.name)
                        else:
                            l.warning("Ignoring LDR at 0x%x", baseaddr) # TODO - can we handle these? 
                            # maybe when we can't dereference, just label it as is, typically points to libc internals and is fixed elsewhere
                            #new_lbl = self.binary.symbol_manager.new_label(addr=baseaddr)
                            #print("0x{:x}:\tldr off_{:x} \t = {:<9}".format(self.insn_addr, baseaddr, "_", new_lbl.name))
                            #self.disp_label = new_lbl

                        #self.binary.register_instruction_reference(self.disp, baseaddr, 'absolute', 4, self.binary.project.arch.name)
                        return
                    else:
                        # TODO: how to handle this? Just like a regular label?
                        #l.warning("Unsupported register-relative arm LDR: register=%s", CAPSTONE_REG_MAP['ARMEL'][self.base])
                        pass

                elif self.mnemonic.startswith(u'str'):
                    pass # TODO?
                    """
                    self.disp_is_coderef, self.disp_is_dataref, baseaddr = \
                        self._imm_to_ptr(self.disp, self.type, self.mnemonic, self.binary.project.arch.name)

                    self.disp_is_coderef = False
                    self.disp_is_dataref = True
                    if self.insn_addr == 0x104d8:
                        import pdb
                        pdb.set_trace()

                    dereferenced_val = self.binary.fast_memory_load(baseaddr, 4, int)
                    if dereferenced_val:
                        new_lbl = self.binary.symbol_manager.new_label(addr=dereferenced_val)
                        print("0x{:x}:\tSTR off_{:x} \t = {:<9} =>  {:<15} == 0x{:x}".format(self.insn_addr, baseaddr, "_", new_lbl.name, dereferenced_val))

                    self.label = self.binary.symbol_manager.new_label(addr=baseaddr)
                    self.label_offset = imm - baseaddr
                    """
                else:
                    l.warning(self.mnemonic)
                    if self.is_coderef or self.is_dataref:
                        self.label = self.binary.symbol_manager.new_label(addr=baseaddr)
                        self.label_offset = imm - baseaddr

                """
                #self.disp_label_offset = self.disp - baseaddr
                self.disp_is_coderef = False
                self.disp_is_dataref = True
                self.binary.register_instruction_reference(self.insn_addr, self.disp, 'relative', self.insn_size, self.binary.project.arch.name)
                self.binary.symbol_manager.addr_to_label[baseaddr].insert(0, self.disp_label)
                print("Add label {} at a2l[0x{:x}] ={} (coderef={}, dataref={})".format(self.disp_label.name, baseaddr, [x.name for x in self.binary.symbol_manager.addr_to_label[baseaddr]], self.disp_is_coderef, self.disp_is_dataref))

                dereference = False #TODO - Is this helping? did we fix this somewhere else?
                if dereference and self.project.arch.name == "ARMEL" and self.mnemonic.startswith(u'ldr') and \
                    self.disp not in self.binary.symbol_manager.addr_to_label.keys():

                    # The memory reference from LDR might be a pointer - how do we handle that?
                    # We need to fixup the disassembly a bit here - we have a memory reference LDR is referencing 
                    self.binary.register_instruction_reference(self.insn_addr, self.disp, 'absolute', self.insn_size, self.binary.project.arch.name)

                    # use a label here: ldr r0, [.label_this]

                    self.disp_label = self.binary.symbol_manager.new_label(addr=self.disp)

                    #self.binary.symbol_manager.addr_to_label[self.disp] = [self.disp_label]
                    self.disp_label_offset = 0
                    l.warning("Found LDR mem at 0x%x - mem 0x%x labeled as %s", self.insn_addr, self.disp, self.disp_label.name)

                    # Dereference the pointer we just labeled and symbolize if necessary
                    dereferenced = self.binary.fast_memory_load(self.disp, 4, int)
                    is_coderef, is_dataref, baseaddr = self._imm_to_ptr(dereferenced, self.type, self.mnemonic)

                    # Symbolize the pointer's value as well, if necessary
                    if is_coderef or is_dataref:
                        deref_lbl = self.binary.symbol_manager.new_label(addr=dereferenced)
                        dereferenced_val = self.binary.fast_memory_load(dereferenced, 4, int)
                        l.warning("\t That's a pointer to 0x%x - labeled as %s", dereferenced, deref_lbl.name)
                        self.binary.register_instruction_reference(self.disp, dereferenced, 'absolute', 4, self.binary.project.arch.name)
                        self.binary.add_data(dereferenced, dereferenced_val)


                        dereferenced2 = self.binary.fast_memory_load(dereferenced, 4, int)
                        #procedure = Procedure(self, f, section=section)
                        #self.procedures.append(procedure)
                        #pdb.set_trace()
                        self.binary.append_data2(deref_lbl, dereferenced2, 4)
                else:
                    self.disp_label = self.binary.symbol_manager.new_label(addr=baseaddr)
                    print("Add label {} at a2l[0x{:x}] (coderef={}, dataref={})".format(self.disp_label.name, baseaddr, self.disp_is_coderef, self.disp_is_dataref))
                    self.disp_label_offset = self.disp - baseaddr
                    self.binary.register_instruction_reference(self.insn_addr, self.disp, 'absolute', self.insn_size, self.binary.project.arch.name)
                """
        elif self.type == OP_TYPE_OTHER:
            # TODO: Add support for other types of armel operands
            l.warning("Ignoring operand of type OP_TYPE_OTHER: %s", capstone_operand.type)


    def _imm_to_ptr(self, imm, operand_type, mnemonic, arch_name=None):  # pylint:disable=no-self-use,unused-argument
        """
        Try to classify an immediate as a pointer.

        :param int imm: The immediate to test.
        :param int operand_type: Operand type of this operand, can either be IMM or MEM.
        :param str mnemonic: Mnemonic of the instruction that this operand belongs to.
        :return: A tuple of (is code reference, is data reference, base address, offset)
        :rtype: tuple
        """

        is_coderef, is_dataref = False, False
        baseaddr = None

        if imm == 0x10000 and self.binary.project.arch.name in ["ARMEL"]: # This is often a constant in ARMEL. TODO: is there a better way?
            return (is_coderef, is_dataref, baseaddr) # TODO - shouldn't this be imm not baseaddr?

        if not is_coderef and not is_dataref:
            if self.binary.main_executable_regions_contain(imm):
                # does it point to the beginning of an instruction?
                if imm in self.binary.all_insn_addrs: # or (arch_name and arch_name == "ARMEL"):
                    is_coderef = True
                    baseaddr = imm

        if not is_coderef and not is_dataref:
            if self.binary.main_nonexecutable_regions_contain(imm):
                is_dataref = True
                baseaddr = imm

        if self.binary.project.arch.name in ['ARMEL']:
            baseaddr = imm
            return (is_coderef, is_dataref, baseaddr)
        else:
            if not is_coderef and not is_dataref:
                tolerance_before = 1024 if operand_type == OP_TYPE_MEM else 64
                contains_, baseaddr_ = self.binary.main_nonexecutable_region_limbos_contain(imm,
                                                                                            tolerance_before=tolerance_before,
                                                                                            tolerance_after=1024
                                                                                            )
                if contains_:
                    is_dataref = True
                    baseaddr = baseaddr_

                if not contains_:
                    contains_, baseaddr_ = self.binary.main_executable_region_limbos_contain(imm)
                    if contains_:
                        is_coderef = True
                        l.warning("Change2 baseaddr from 0x%x to 0x%x", imm, baseaddr_)
                        baseaddr = baseaddr_

        return (is_coderef, is_dataref, baseaddr)

