#[macro_export]
macro_rules! impl_vector {
    ($( $VECX:ident: $PACVECX:ty, )+) => { $(
        pub struct $VECX {
            registers: $PACVECX,
            px_div: u32,
        }

        impl $VECX {
            pub fn new(registers: $PACVECX) -> Self {
                let ppv = registers.pixels_per_volt().read().pixels_per_volt().bits() as u32;
                let px_div = ppv >> tiliqua_lib::scope::VScale::Scale1V.to_scale_bits();
                Self { registers, px_div }
            }

            pub fn pixels_per_div(&self) -> (u32, u32) {
                (self.px_div, self.px_div)
            }

            pub fn set_hue(&mut self, hue: u8) {
                self.registers.hue().write(|w| unsafe { w.hue().bits(hue) });
            }

            pub fn set_intensity(&mut self, intensity: u8) {
                self.registers.intensity().write(|w| unsafe { w.intensity().bits(intensity) });
            }

            pub fn set_xscale(&mut self, vs: tiliqua_lib::scope::VScale) {
                self.registers.xscale().write(|w| unsafe { w.scale().bits(vs.to_scale_bits()) });
            }

            pub fn set_yscale(&mut self, vs: tiliqua_lib::scope::VScale) {
                self.registers.yscale().write(|w| unsafe { w.scale().bits(vs.to_scale_bits()) });
            }

            pub fn set_xoffset_px(&mut self, pos_px: i16) {
                self.registers.xoffset().write(|w| unsafe { w.value().bits(pos_px as u16) });
            }

            pub fn set_yoffset_px(&mut self, pos_px: i16) {
                self.registers.yoffset().write(|w| unsafe { w.value().bits(pos_px as u16) });
            }

            pub fn set_pscale(&mut self, scale: u8) {
                self.registers.pscale().write(|w| unsafe { w.scale().bits(0xf - scale) });
            }

            pub fn set_cscale(&mut self, scale: u8) {
                self.registers.cscale().write(|w| unsafe { w.scale().bits(0xf - scale) });
            }

            pub fn set_enabled(&mut self, enabled: bool) {
                self.registers.flags().write(|w| w.enable().bit(enabled));
            }
        }
    )+ };
}
