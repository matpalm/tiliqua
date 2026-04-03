/// Unified persistence control.
///
/// Maps a single 1-80 value to decay, holdoff and probabilistic skip:
///   1-15:  decay ramps 15->1, holdoff=32, skip=0
///   16-64: decay=1, holdoff=32, skip ramps up
///   65-80: decay=1, holdoff ramps 32->256, skip continues ramping
pub trait Persist {
    fn set_persistence(&mut self, value: u8);
}

#[macro_export]
macro_rules! impl_persist {
    ($(
        $PERSISTX:ident: $PACPERSISTX:ty,
    )+) => {
        $(
            #[derive(Debug)]
            pub struct $PERSISTX {
                registers: $PACPERSISTX,
            }

            impl $PERSISTX {
                pub fn new(registers: $PACPERSISTX) -> Self {
                    Self { registers }
                }

                fn set_holdoff(&mut self, value: u16)  {
                    self.registers.persist().write(|w| unsafe { w.persist().bits(value) } );
                }

                fn set_decay(&mut self, value: u8)  {
                    self.registers.decay().write(|w| unsafe { w.decay().bits(value) } );
                }

                fn set_skip(&mut self, value: u8)  {
                    self.registers.skip().write(|w| unsafe { w.skip().bits(value) } );
                }
            }

            impl hal::persist::Persist for $PERSISTX {
                fn set_persistence(&mut self, value: u8) {
                    let p = value as u32;
                    if p <= 15 {
                        self.set_decay((16 - p) as u8);
                        self.set_holdoff(32);
                        self.set_skip(0);
                    } else {
                        let t = p - 16; // 0..64
                        self.set_decay(1);
                        self.set_skip(core::cmp::min(t << 2, 255) as u8);
                        if p <= 64 {
                            self.set_holdoff(32);
                        } else {
                            let h = p - 65; // 0..15
                            self.set_holdoff(core::cmp::min(32 + (h << 5), 256) as u16);
                        }
                    }
                }
            }
        )+
    };
}
