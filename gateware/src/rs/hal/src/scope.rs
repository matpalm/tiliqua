#[macro_export]
macro_rules! impl_scope {
    ($( $SCOPEX:ident: $PACSCOPEX:ty, )+) => { $(
        pub struct $SCOPEX {
            registers: $PACSCOPEX,
            xscale: u8,
            px_div_x: u32,
            px_div_y: u32,
            fs_up: u32,
        }

        impl $SCOPEX {
            pub fn new(registers: $PACSCOPEX, xscale: u8) -> Self {
                let ppv = registers.pixels_per_volt().read().pixels_per_volt().bits() as u32;
                let fs_up = registers.fs().read().fs().bits();
                let px_div_x = ppv >> xscale;
                let px_div_y = ppv >> tiliqua_lib::scope::VScale::Scale1V.to_scale_bits();
                registers.xscale().write(|w| unsafe { w.xscale().bits(xscale) });
                Self { registers, xscale, px_div_x, px_div_y, fs_up }
            }

            pub fn pixels_per_div(&self) -> (u32, u32) {
                (self.px_div_x, self.px_div_y)
            }

            pub fn set_timebase(&mut self, tb: tiliqua_lib::scope::Timebase) {
                let numer: u64 = (self.px_div_x as u64) * (1u64 << (15 + self.xscale as u32));
                let raw = (numer * 1_000_000 / (self.fs_up as u64 * tb.t_div_us())) as u32;
                self.registers.timebase().write(|w| unsafe { w.timebase().bits(raw) });
            }

            pub fn set_yscale(&mut self, vs: tiliqua_lib::scope::VScale) {
                self.registers.yscale().write(|w| unsafe { w.yscale().bits(vs.to_scale_bits()) });
            }

            pub fn set_hue(&mut self, hue: u8) {
                self.registers.hue().write(|w| unsafe { w.hue().bits(hue) });
            }

            pub fn set_intensity(&mut self, intensity: u8) {
                self.registers.intensity().write(|w| unsafe { w.intensity().bits(intensity) });
            }

            pub fn set_trigger_level(&mut self, lvl: i16) {
                self.registers.trigger_lvl().write(|w| unsafe { w.trigger_level().bits(lvl as u16) });
            }

            pub fn set_ypos_px(&mut self, ch: usize, pos: i16) {
                match ch {
                    0 => self.registers.ypos0().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    1 => self.registers.ypos1().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    2 => self.registers.ypos2().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    3 => self.registers.ypos3().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    _ => return,
                };
            }

            pub fn set_xscale(&mut self, scale: u8) {
                self.xscale = scale;
                let ppv = self.registers.pixels_per_volt().read().pixels_per_volt().bits() as u32;
                self.px_div_x = ppv >> scale;
                self.registers.xscale().write(|w| unsafe { w.xscale().bits(scale) });
            }

            pub fn set_xpos_px(&mut self, pos: i16) {
                self.registers.xpos().write(|w| unsafe { w.xpos().bits(pos as u16) });
            }

            pub fn set_enabled(&mut self, enabled: bool, trigger_always: bool) {
                self.registers.flags().write(|w| {
                    w.enable().bit(enabled);
                    w.trigger_always().bit(trigger_always)
                });
            }
        }
    )+ };
}
