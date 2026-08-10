Tutorial 5: Using PMODs
=======================

Any existing PMODs can be used with Tiliqua to add extra functionality.

For example, a 8 bit DIP switch and LED strip.

.. image:: /_static/pmod_dip_off.png
  :width: 640

We can configure Tiliqua to read the DIP switches and control the LEDs with Platform resources.

In this setup we have the DIP switches connected to `ex1` ( using `dir="i"` )
and the LED strip connected via `ex0` ( using `dir="o"` )

.. code-block:: text
        platform.add_resources(
            [
                Resource(
                    "ex1_dip",
                    0,
                    Subsignal("pin1", Pins("1", dir="i", conn=("pmod", 1))),
                    Attrs(IO_TYPE="LVCMOS33"),
                ),
                Resource(
                    "ex0_led",
                    0,
                    Subsignal("pin1", Pins("1", dir="o", conn=("pmod", 0))),
                    Attrs(IO_TYPE="LVCMOS33"),
                ),
            ]
        )
        ex1_dip = platform.request("ex1_dip", 0)
        ex0_led = platform.request("ex0_led", 0)


We can use the DIP switch pin1 to turn on the first LED in the strip.

.. code-block:: text
        # The dip switch is asynchronous to our clock domain so we need
        # to make sure it is stable.
        dip_pin1_sync = Signal()
        m.submodules.dip_pin1_sync = FFSynchronizer(ex1_dip.pin1.i, dip_pin1_sync)

        # Turn on LED when dip switch set.
        m.d.comb += [
            ex0_led.pin1.o.eq(dip_pin1_sync),
        ]

.. image:: /_static/pmod_dip_on.png
  :width: 640

We might also use the DIP switch setting to decide to invert an input in the mirror example

.. code-block:: text
        # Invert channel 0 when dip switch set.
        ch0_in = self.i.payload[0].as_value()
        ch0_out = Signal.like(ch0_in)
        m.d.comb += [
            ch0_out.eq(Mux(dip_pin1_sync, -ch0_in, ch0_in)),
            self.o.payload[0].eq(ch0_out),
        ]