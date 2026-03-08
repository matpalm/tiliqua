use heapless::Vec;

#[derive(Clone, Copy)]
pub enum CcMapMode {
    Absolute,
    Decrement,
    Increment,
}

#[derive(Clone, Copy)]
pub struct CcMapping<P: Copy> {
    pub cc: u8,
    pub page: P,
    pub option_key: u32,
    pub mode: CcMapMode,
}

pub struct CcAction<P: Copy> {
    pub page: P,
    pub option_key: u32,
    pub cc_value: u8,
    pub mode: CcMapMode,
}

pub struct MidiCcMapper<P: Copy, const N: usize> {
    mappings: Vec<CcMapping<P>, N>,
}

impl<P: Copy + PartialEq, const N: usize> MidiCcMapper<P, N> {
    pub fn new() -> Self {
        Self { mappings: Vec::new() }
    }

    pub fn add(&mut self, cc: u8, page: P, option_key: u32, mode: CcMapMode) {
        self.mappings.push(CcMapping { cc, page, option_key, mode }).ok();
    }

    pub fn process(&self, cc_num: u8, cc_val: u8) -> Option<CcAction<P>> {
        self.mappings.iter()
            .find(|m| m.cc == cc_num)
            .map(|m| CcAction {
                page: m.page,
                option_key: m.option_key,
                cc_value: cc_val,
                mode: m.mode,
            })
    }
}
