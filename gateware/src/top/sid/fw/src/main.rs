#![no_std]
#![no_main]

use critical_section::Mutex;
use core::convert::TryInto;
use log::info;
use riscv_rt::entry;
use irq::handler;
use core::cell::RefCell;

use tiliqua_pac as pac;
use tiliqua_hal as hal;
use tiliqua_fw::*;
use tiliqua_lib::*;
use tiliqua_lib::generated_constants::*;

use embedded_graphics::{
    pixelcolor::{Gray8, GrayColor},
    prelude::*,
};

use opts::Options;
use hal::pca9635::Pca9635Driver;

impl_ui!(UI,
         Options,
         Encoder0,
         Pca9635Driver<I2c0>,
         EurorackPmod0);

tiliqua_hal::impl_dma_display!(DMADisplay, H_ACTIVE, V_ACTIVE, VIDEO_ROTATE_90);

pub const TIMER0_ISR_PERIOD_MS: u32 = 5;

struct App {
    ui: UI,
}

impl App {
    pub fn new(opts: Options) -> Self {
        let peripherals = unsafe { pac::Peripherals::steal() };
        let encoder = Encoder0::new(peripherals.ENCODER0);
        let i2cdev = I2c0::new(peripherals.I2C0);
        let pca9635 = Pca9635Driver::new(i2cdev);
        let pmod = EurorackPmod0::new(peripherals.PMOD0_PERIPH);
        Self {
            ui: UI::new(opts, TIMER0_ISR_PERIOD_MS,
                        encoder, pca9635, pmod),
        }
    }
}

fn timer0_handler(app: &Mutex<RefCell<App>>) {

    use tiliqua_fw::opts::{VoiceOptions, ModulationTarget, VoiceModulationType};

    let peripherals = unsafe { pac::Peripherals::steal() };
    let sid = peripherals.SID_PERIPH;
    let sid_poke = |_sid: &pac::SID_PERIPH, addr: u8, data: u8| {
        _sid.transaction_data().write(
            |w| unsafe { w.transaction_data().bits(((data as u16) << 5) | (addr as u16)) } );
    };

    critical_section::with(|cs| {
        let mut app = app.borrow_ref_mut(cs);
        app.ui.update();

        let voices: [&VoiceOptions; 3] = [
            &opts.voice1,
            &opts.voice2,
            &opts.voice3,
        ];

        let mods: [ModulationTarget; 4] = [
            opts.modulate.in0.value,
            opts.modulate.in1.value,
            opts.modulate.in2.value,
            opts.modulate.in3.value,
        ];

        for n_voice in 0usize..3usize {

            let base = (7*n_voice) as u8;

            // MODULATION

            let mut freq: u16 = voices[n_voice].freq.value;
            let mut gate = voices[n_voice].gate.value;
            for (ch, m) in mods.iter().enumerate() {
                if let Some(VoiceModulationType::Frequency) = m.modulates_voice(n_voice) {
                    let volts: f32 = (x[ch] as f32) / 4096.0f32;
                    let freq_hz = volts_to_freq(volts);
                    freq = 16u16 * (0.05960464f32 * freq_hz) as u16; // assumes 1Mhz SID clk
                                                                     // http://www.sidmusic.org/sid/sidtech2.html
                }
                if let Some(VoiceModulationType::Gate) = m.modulates_voice(n_voice) {
                    if x[ch] > 2000 {
                        gate = 1;
                    }
                    if x[ch] < 1000 {
                        gate = 0;
                    }
                }
            }

            // Propagate modulation back to menu system

            /*
            voices[n_voice].freq.value = freq;
            voices[n_voice].gate.value = gate;
            */

            freq = (freq as f32 * (voices[n_voice].freq_os.value as f32 / 1000.0f32)) as u16;

            sid_poke(&sid, base+0, freq as u8);
            sid_poke(&sid, base+1, (freq>>8) as u8);

            sid_poke(&sid, base+2, voices[n_voice].pw.value as u8);
            sid_poke(&sid, base+3, (voices[n_voice].pw.value>>8) as u8);


            let mut reg04 = 0u8;
            use crate::opts::Wave;
            match voices[n_voice].wave.value {
                Wave::Triangle => { reg04 |= 0x10; }
                Wave::Saw      => { reg04 |= 0x20; }
                Wave::Pulse    => { reg04 |= 0x40; }
                Wave::Noise    => { reg04 |= 0x80; }
            }

            reg04 |= gate;
            reg04 |= voices[n_voice].sync.value << 1;
            reg04 |= voices[n_voice].ring.value << 2;

            sid_poke(&sid, base+4, reg04);

            sid_poke(&sid, base+5,
                voices[n_voice].decay.value |
                (voices[n_voice].attack.value << 4));

            sid_poke(&sid, base+6,
                voices[n_voice].release.value |
                (voices[n_voice].sustain.value << 4));
        }

        sid_poke(&sid, 0x15, (opts.filter.cutoff.value & 0x7) as u8);
        sid_poke(&sid, 0x16, (opts.filter.cutoff.value >> 3) as u8);
        sid_poke(&sid, 0x17,
            (opts.filter.filt1.value |
            (opts.filter.filt2.value << 1) |
            (opts.filter.filt3.value << 2) |
            (opts.filter.reso.value  << 4)) as u8
            );
        sid_poke(&sid, 0x18,
            ((opts.filter.lp.value     << 4) |
             (opts.filter.bp.value     << 5) |
             (opts.filter.hp.value     << 6) |
             (opts.filter.v3off.value  << 7) |
             (opts.filter.volume.value << 0)) as u8
            );
    });
}

pub fn write_palette(video: &mut Video0, p: palette::ColorPalette) {
    for i in 0..PX_INTENSITY_MAX {
        for h in 0..PX_HUE_MAX {
            let rgb = palette::compute_color(i, h, p);
            video.set_palette_rgb(i as u8, h as u8, rgb.r, rgb.g, rgb.b);
        }
    }
}

#[entry]
fn main() -> ! {
    let peripherals = pac::Peripherals::take().unwrap();

    let sysclk = pac::clock::sysclk();
    let serial = Serial0::new(peripherals.UART0);
    let mut timer = Timer0::new(peripherals.TIMER0, sysclk);
    let mut video = Video0::new(peripherals.VIDEO_PERIPH);
    let mut display = DMADisplay {
        framebuffer_base: PSRAM_FB_BASE as *mut u32,
    };

    tiliqua_fw::handlers::logger_init(serial);

    info!("Hello from Tiliqua XBEAM!");

    let opts = opts::Options::new();
    let mut last_palette = opts.beam.palette.value;
    let app = Mutex::new(RefCell::new(App::new(opts)));

    handler!(timer0 = || timer0_handler(&app));

    irq::scope(|s| {

        s.register(handlers::Interrupt::TIMER0, timer0);

        timer.enable_tick_isr(TIMER0_ISR_PERIOD_MS,
                              pac::Interrupt::TIMER0);

        let vscope  = peripherals.VECTOR_PERIPH;
        let scope  = peripherals.SCOPE_PERIPH;
        let mut first = true;

        let sid = peripherals.SID_PERIPH;
        let sid_poke = |_sid: &pac::SID_PERIPH, addr: u8, data: u8| {
            _sid.transaction_data().write(
                |w| unsafe { w.transaction_data().bits(((data as u16) << 5) | (addr as u16)) } );
        };
        sid_poke(&sid, 24,15);    /* Turn up the volume */
        sid_poke(&sid, 5,0);      /* Fast Attack, Decay */
        sid_poke(&sid, 5+7,0);      /* Fast Attack, Decay */
        sid_poke(&sid, 5+14,0);      /* Fast Attack, Decay */
        sid_poke(&sid, 6,0xF0);      /* Full volume on sustain, quick release */
        sid_poke(&sid, 6+7,0xF0);    /* Full volume on sustain, quick release */
        sid_poke(&sid, 6+14,0xF0);   /* Full volume on sustain, quick release */
        let freq: u16 = 2000;
        sid_poke(&sid, 0, freq as u8);
        sid_poke(&sid, 1, (freq>>8) as u8);
        sid_poke(&sid, 4, 0x11);   /* Enable gate, triangel waveform. */

        loop {

            let opts = critical_section::with(|cs| {
                app.borrow_ref(cs).ui.opts.clone()
            });

            if opts.beam.palette.value != last_palette || first {
                write_palette(&mut video, opts.beam.palette.value);
                last_palette = opts.beam.palette.value;
            }

            draw::draw_options(&mut display, &opts, H_ACTIVE-200, V_ACTIVE/2, opts.beam.hue.value).ok();
            draw::draw_name(&mut display, H_ACTIVE/2, V_ACTIVE-50, opts.beam.hue.value, UI_NAME, UI_SHA).ok();

            let hl_wfm: Option<u8> = match opts_ro.screen.value {
                opts::Screen::Voice1 => Some(0),
                opts::Screen::Voice2 => Some(1),
                opts::Screen::Voice3 => Some(2),
                _ => None,
            };

            let gates: [bool; 3] = [
                opts_ro.voice1.gate.value == 1,
                opts_ro.voice2.gate.value == 1,
                opts_ro.voice3.gate.value == 1,
            ];

            let switches: [bool; 3] = [
                opts_ro.filter.filt1.value == 1,
                opts_ro.filter.filt2.value == 1,
                opts_ro.filter.filt3.value == 1,
            ];

            let filter_types: [bool; 3] = [
                opts_ro.filter.lp.value == 1,
                opts_ro.filter.bp.value == 1,
                opts_ro.filter.hp.value == 1,
            ];

            let hl_filter: bool = opts_ro.screen.value == opts::Screen::Filter;

            draw::draw_sid(&mut display, 100, V_ACTIVE/4+25, hue, hl_wfm, gates, hl_filter, switches, filter_types);

            {
                let font_small_white = MonoTextStyle::new(&FONT_9X15_BOLD, Gray8::new(0xB0 + hue));
                let hc = (H_ACTIVE/2) as i16;
                let vc = (V_ACTIVE/2) as i16;
                Text::new(
                    "out3: combined, post-filter",
                    Point::new((opts_ro.scope.xpos.value + hc - 250) as i32,
                               (opts_ro.scope.ypos0.value + vc + 50) as i32),
                    font_small_white,
                )
                .draw(&mut display).ok();
                Text::new(
                    "out0: voice 1, post-VCA",
                    Point::new((opts_ro.scope.xpos.value + hc - 250) as i32,
                               (opts_ro.scope.ypos1.value + vc + 50) as i32),
                    font_small_white,
                )
                .draw(&mut display).ok();
                Text::new(
                    "out1: voice 2, post-VCA",
                    Point::new((opts_ro.scope.xpos.value + hc - 250) as i32,
                               (opts_ro.scope.ypos2.value + vc + 50) as i32),
                    font_small_white,
                )
                .draw(&mut display).ok();
                Text::new(
                    "out2: voice 3, post-VCA",
                    Point::new((opts_ro.scope.xpos.value + hc - 250) as i32,
                               (opts_ro.scope.ypos3.value + vc + 50) as i32),
                    font_small_white,
                )
                .draw(&mut display).ok();
            }

            video.set_persist(opts.beam.persist.value);
            video.set_decay(opts.beam.decay.value);

            vscope.hue().write(|w| unsafe { w.hue().bits(opts.beam.hue.value) } );
            vscope.intensity().write(|w| unsafe { w.intensity().bits(opts.beam.intensity.value) } );
            vscope.xscale().write(|w| unsafe { w.xscale().bits(opts.vector.xscale.value) } );
            vscope.yscale().write(|w| unsafe { w.yscale().bits(opts.vector.yscale.value) } );

            scope.hue().write(|w| unsafe { w.hue().bits(opts.beam.hue.value) } );
            scope.intensity().write(|w| unsafe { w.intensity().bits(opts.beam.intensity.value) } );

            scope.trigger_lvl().write(|w| unsafe { w.trigger_level().bits(opts.scope.trigger_lvl.value as u16) } );
            scope.xscale().write(|w| unsafe { w.xscale().bits(opts.scope.xscale.value) } );
            scope.yscale().write(|w| unsafe { w.yscale().bits(opts.scope.yscale.value) } );
            scope.timebase().write(|w| unsafe { w.timebase().bits(opts.scope.timebase.value) } );

            scope.ypos0().write(|w| unsafe { w.ypos().bits(opts.scope.ypos0.value as u16) } );
            scope.ypos1().write(|w| unsafe { w.ypos().bits(opts.scope.ypos1.value as u16) } );
            scope.ypos2().write(|w| unsafe { w.ypos().bits(opts.scope.ypos2.value as u16) } );
            scope.ypos3().write(|w| unsafe { w.ypos().bits(opts.scope.ypos3.value as u16) } );

            scope.trigger_always().write(
                |w| w.trigger_always().bit(opts.scope.trigger_mode.value == opts::TriggerMode::Always) );

            if opts.screen.value == opts::Screen::Vector {
                scope.en().write(|w| w.enable().bit(false) );
                vscope.en().write(|w| w.enable().bit(true) );
            }

            if opts.screen.value == opts::Screen::Scope {
                scope.en().write(|w| w.enable().bit(true) );
                vscope.en().write(|w| w.enable().bit(false) );
            }

            first = false;
        }
    })
}
