"""Write ipywidgets like React

ReactJS - ipywidgets relation:
 * DOM nodes -- Widget
 * Element -- Element
 * Component -- function

"""

import copy
import logging
import sys
import threading
from dataclasses import dataclass, field
from inspect import isclass
from types import TracebackType
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

import ipywidgets as widgets

from . import _version

__version__ = _version.__version__

_last_rc = None  # used for testing
T = TypeVar("T")
U = TypeVar("U")
W = TypeVar("W")  # used for widgets
E = TypeVar("E")  # used for elements

WidgetOrList = Union[widgets.Widget, List[widgets.Widget]]
EffectCleanupCallable = Callable[[], None]
EffectCallable = Callable[[], Optional[EffectCleanupCallable]]
ROOT_KEY = "ROOT::"
logger = logging.getLogger("react")  # type: ignore

# this will show friendly stack traces
DEBUG = 0
# if True, will show the original stacktrace as cause
TRACEBACK_ORIGINAL = True
MIME_WIDGETS = "application/vnd.jupyter.widget-view+json"


widget_render_error_msg = (
    """Cannot show widget. You probably want to rerun the code cell above (<i>Click in the code cell, and press Shift+Enter <kbd>⇧</kbd>+<kbd>↩</kbd></i>)."""
)

mime_bundle_default = {"text/plain": "Cannot show ipywidgets in text", "text/html": widget_render_error_msg}


def element(cls, **kwargs):
    return ComponentWidget(cls)(**kwargs)


widgets.Widget.element = classmethod(element)


def join_key(parent_key, key):
    return f"{parent_key}{key}"


def pp(o):
    import prettyprinter

    prettyprinter.install_extras()

    prettyprinter.pprint(o, width=1)


def same_component(c1, c2):
    # return (c1.f.__name__ == c2.f.__name__) and (c1.f.__module__ == c2.f.__module__)
    return c1 == c2


class ComponentCreateError(RuntimeError):
    def __init__(self, rich_traceback):
        super().__init__(rich_traceback)
        self.rich_traceback = rich_traceback


class Component:
    name: str

    def __call__(self, *args, **kwargs) -> Union[widgets.Widget, "Element"]:
        pass


class Element(Generic[W]):
    def __init__(self, component, *args, **kwargs):
        self.component = component
        self.mime_bundle = mime_bundle_default
        self._key: Optional[str] = None
        self.args = args
        self.kwargs = kwargs
        self.handlers = []
        self._current_context = None
        rc = _get_render_context(required=False)
        if rc:
            self._current_context = rc.context
        if rc is not None and rc.container_adders:
            rc.container_adders[-1].add(self)
        if DEBUG:
            # since we construct widgets or components from a different code path
            # we want to preserve the original call stack, by manually tracking frames
            try:
                assert False
            except AssertionError:
                self.traceback = cast(TracebackType, sys.exc_info()[2])

            assert self.traceback is not None
            assert self.traceback.tb_frame is not None
            assert self.traceback.tb_frame.f_back is not None
            frame_py = self.traceback.tb_frame.f_back.f_back
            assert frame_py is not None
            self.traceback = TracebackType(tb_frame=frame_py, tb_lasti=self.traceback.tb_lasti, tb_lineno=frame_py.f_lineno, tb_next=None)

    def key(self, value: str):
        """Returns the same element with a custom key set.

        This can help render performance. See documentation for details.
        """
        self._key = value
        return self

    def split_kwargs(self, kwargs):
        listeners = {}
        normal_kwargs = {}
        assert isinstance(self.component, ComponentWidget)
        args = self.component.widget.class_trait_names()
        for name, value in kwargs.items():
            if name.startswith("on_") and name not in args:
                listeners[name[3:]] = value
            else:
                normal_kwargs[name] = value
        return normal_kwargs, listeners

    def handle_custom_kwargs(self, widget: widgets.widgets.Widget, kwargs):
        listeners = kwargs
        for name, listener in listeners.items():

            def add_event_handler(name=name, listener=listener):
                def event_handler(change):
                    listener(change.new)

                def cleanup():
                    widget.unobserve(event_handler, name)

                widget.observe(event_handler, name)
                return cleanup

            use_side_effect(add_event_handler)

    def __repr__(self):
        args = [f"{value!r}" for value in self.args]
        kwargs = [f"{key} = {value!r}" for key, value in self.kwargs.items()]
        args_formatted = ", ".join(args + kwargs)
        if isinstance(self.component, ComponentFunction):
            name = self.component.f.__name__
            return f"{name}({args_formatted})"
        if isinstance(self.component, ComponentWidget):
            name = self.component.widget.__module__ + "." + self.component.widget.__name__
            return f"{name}.element({args_formatted})"
        else:
            raise RuntimeError(f"No repr for {type(self)}")

    def on(self, name, callback):
        self.handlers.append((name, callback))
        return self

    def _ipython_display_(self, **kwargs):
        display(self, self.mime_bundle)

    def __enter__(self):
        rc = _get_render_context()
        ca = ContainerAdder[T](self, "children")
        assert rc.context is self._current_context, f"Context change from {self._current_context} -> {rc.context}"
        assert rc.context is not None
        rc.container_adders.append(ca)
        return self

    def __exit__(self, *args, **kwargs):

        rc = _get_render_context()
        assert rc.context is self._current_context, f"Context change from {self._current_context} -> {rc.context}"
        assert rc.context is not None
        ca = rc.container_adders.pop()
        ca.assign()


FuncT = TypeVar("FuncT", bound=Callable[..., Element])


def find_children(el):
    children = set()
    if not isinstance(el.kwargs, dict):
        raise RuntimeError("keyword arguments for {el} should be a dict, not {el.kwargs}")
    for arg in list(el.kwargs.values()) + list(el.args):
        if isinstance(arg, Element):
            children.add(arg)
        elif isinstance(arg, (tuple, list)):
            for child in arg:
                if isinstance(child, Element):
                    children.add(child)
                    children |= find_children(child)
        elif isinstance(arg, dict):
            for child in arg.values():
                if isinstance(child, Element):
                    children.add(child)
                    children |= find_children(child)
    return children


class ContainerAdder(Generic[W]):
    def __init__(self, el: Element[W], prop_name: str):
        self.el = el
        self.prop_name = prop_name
        self.created: List[Element] = []

    def add(self, el):
        self.created.append(el)

    def assign(self):
        children = set()
        for el in self.created:
            children |= find_children(el)
        top_level = [k for k in self.created if k not in children]
        if self.prop_name not in self.el.kwargs:
            self.el.kwargs[self.prop_name] = []
        # generic way to add to a list or tuple
        container_prop_type = type(self.el.kwargs[self.prop_name])
        self.el.kwargs[self.prop_name] = self.el.kwargs[self.prop_name] + container_prop_type(top_level)


class ComponentWidget(Component):
    def __init__(self, widget: Type[widgets.Widget], mime_bundle=mime_bundle_default):
        self.mime_bundle = mime_bundle
        self.widget = widget
        self.name = widget.__name__

    def __repr__(self):
        return f"Component[{self.widget!r}]"

    def __call__(self, *args, **kwargs):
        el: Element = Element(self, *args, **kwargs)
        # TODO: temporary, we cannot change the constructor
        # otherwise we need to generate the wrapper code again for all libraries
        el.mime_bundle = self.mime_bundle
        return el


class ComponentFunction(Component):
    def __init__(self, f: Callable[[], Element], mime_bundle=mime_bundle_default):
        self.f = f
        self.name = self.f.__name__
        self.mime_bundle = mime_bundle

    def __repr__(self):
        return f"react.component({self.f.__module__}.{self.f.__name__})"

    def __call__(self, *args, **kwargs):
        el: Element = Element(self, *args, **kwargs)
        el.mime_bundle = self.mime_bundle
        return el


@overload
def component(obj: None = None, mime_bundle=...) -> Callable[[FuncT], FuncT]:
    ...


@overload
def component(obj: FuncT, mime_bundle=...) -> FuncT:
    ...


# it is actually this...
# def component(obj: Union[Type[widgets.Widget], FuncT]) -> Union[ComponentWidget, ComponentFunction[FuncT]]:
# but casting to FuncT gives much better type hints (e.g. argument types checks etc)


def component(obj: FuncT = None, mime_bundle: Dict[str, Any] = mime_bundle_default):
    def wrapper(obj: FuncT) -> FuncT:
        if isclass(obj) and issubclass(obj, widgets.Widget):
            return cast(FuncT, ComponentWidget(widget=obj, mime_bundle=mime_bundle))
        else:
            return cast(FuncT, ComponentFunction(f=obj, mime_bundle=mime_bundle))

    if obj is not None:
        return wrapper(obj)
    else:
        return wrapper


def force_update():
    rc = _get_render_context()
    rc.force_update()


def get_widget(el: Element):
    """Returns the real underlying widget, can only be used in use_side_effect"""
    rc = _get_render_context()
    if el not in rc._widgets:
        if id(el) in rc._old_element_ids:
            raise KeyError(f"Element {el} was found to be in a previous render, you may have used a stale element")
        else:
            raise KeyError(f"Element {el} not found in all known widgets for the component {rc._widgets}")
    return rc._widgets[el]


def use_state(initial: T, key: str = None, eq: Callable[[Any, Any], bool] = None) -> Tuple[T, Callable[[Union[T, Callable[[T], T]]], T]]:
    """Returns a (value, setter) tuple that is used to manage state in a component.

    This function can only be called from a component function.

    The value rturns the current state (which equals initial at the first render call).
    Or the value that was last

    Subsequent
    """
    global _rc
    if _rc is None:
        raise RuntimeError("No render context")
    return _rc.use_state(initial, key, eq)


def use_side_effect(effect: EffectCallable, dependencies=None):
    global _rc
    if _rc is None:
        raise RuntimeError("No render context")
    return _rc.use_side_effect(effect, dependencies=dependencies)


def use_state_widget(widget: widgets.Widget, prop_name, key=None):
    global _rc
    if _rc is None:
        raise RuntimeError("No render context")
    initial_value = getattr(widget, prop_name)
    value, setter = use_state(initial_value, key=key)
    if _rc.first_render:

        def handler(change):
            setter(change.new)  # type: ignore

        widget.observe(handler, prop_name)
    return value


@overload
def _get_render_context(required: Literal[True] = ...) -> "_RenderContext":
    ...


@overload
def _get_render_context(required: Literal[False] = ...) -> Optional["_RenderContext"]:
    ...


def _get_render_context(required=True):
    global _rc
    if _rc is None and required:
        raise RuntimeError("No render context")
    return _rc


def use_reducer(reduce: Callable[[T, U], T], initial_state: T) -> Tuple[T, Callable[[U], None]]:
    state, set_state = use_state(initial_state)

    def dispatch(action):
        def state_updater(state):
            return reduce(state, action)
        set_state(state_updater)

    return state, dispatch


def use_context(key: str):
    rc = _get_render_context()
    value = None
    context = rc.context
    while value is None and context is not None:
        value = context.user_contexts.get(key)
        context = context.parent
    if value is None:
        raise KeyError(f"No value found in element or parent element under key {key}")
    return value


def use_memo(f, debug_name: str = None, args: Optional[List] = None, kwargs: Optional[Dict] = None):
    if debug_name is None:
        debug_name = f.__name__
    rc = _get_render_context()
    if args is None and kwargs is None:

        def wrapper(*args, **kwargs):
            return rc.use_memo(f, args, kwargs, debug_name)

        return wrapper
    else:
        return rc.use_memo(f, args, kwargs, debug_name)


def use_callback(f, dependencies):
    def wrapper(*ignore):
        return f

    use_memo(wrapper, args=dependencies)


class Ref(Generic[T]):
    def __init__(self, initial_value: T):
        self.current = initial_value


def use_ref(initial_value: T) -> Ref[T]:
    def make_ref():
        return Ref(initial_value)

    ref = use_memo(make_ref, args=[])
    return ref


def provide_context(key: str, obj: Any):
    rc = _get_render_context()
    context = rc.context
    assert context is not None
    context.user_contexts[key] = obj


"""
# naming:

# this is a component
@react.component
def Child(children=[])
    # this is the render function
    return w.VBox(children=children)  # it returns the root element

# this is also a component
@react.component
def App():
    # element1 will go into elements_next on render
    # and move to elements during reconciliation
    element1 = w.Button(description="Hi")
    element = Child(children=[element1])
    return element

invoke_element = App()
"""


@dataclass
class ComponentContext:
    parent: Optional["ComponentContext"] = field(default=None, repr=False)

    # this is the element in the parent context
    invoke_element: Optional[Element] = None

    # the root element for this component
    root_element: Optional[Element] = None
    # all elements, including the root element
    elements_next: Dict[str, Element] = field(default_factory=dict)
    # from previous reconciliation phase
    elements: Dict[str, Element] = field(default_factory=dict)
    # contexts for child elements which are a component
    # (every function component should be in children and elements, but not widget component)
    children_next: Dict[str, "ComponentContext"] = field(default_factory=dict)
    # from previous reconciliation phase, so we can reuse hooks
    children: Dict[str, "ComponentContext"] = field(default_factory=dict)

    # hooks data
    state: Dict = field(default_factory=dict)
    state_index = 0
    effects: List["Effect"] = field(default_factory=list)
    effect_index = 0
    memo: List[Any] = field(default_factory=list)
    memo_index = 0
    # for provide/use_context
    user_contexts: Dict[Any, Any] = field(default_factory=dict)

    # to track key collisions
    used_keys: Set[str] = field(default_factory=set)
    # needs_render: bool = False

    # elements created in this context go there
    owns: Set[Element] = field(default_factory=set)


TEffect = TypeVar("TEffect", bound="Effect")


class Effect:
    def __init__(self, callable: EffectCallable, dependencies: Optional[List[Any]] = None, next: Optional["Effect"] = None) -> None:
        self.callable = callable
        self.dependencies = dependencies
        self.cleanup: Optional[EffectCleanupCallable] = None
        self.next = next
        self.executed = False

    def __call__(self):
        if self.executed:
            return
        self.cleanup = self.callable()
        self.executed = True


class _RenderContext:
    context: Optional[ComponentContext] = None

    def __init__(self, element: Element, container: widgets.Widget = None, children_trait="children", handle_error: bool = True, initial_state=None):
        self.element = element
        self.container = container
        self.children_next_trait = children_trait
        self.first_render = True
        self.container_adders: List[ContainerAdder] = []
        self.context = ComponentContext()
        self.context_root = self.context
        self.render_count = 0
        self.last_root_widget: widgets.Widget = None
        self._is_rendering = False
        self._state_changed = False
        self._state_changed_reason: Optional[str] = None
        self.thread_lock = threading.Lock()
        self.tracebacks: List[TracebackType] = []
        self.handle_error = handle_error
        if initial_state:
            self.state_set(self.context_root, initial_state)
        self._widgets: Dict[Element, widgets.Widget] = {}

        # each render phase, we track which elements we proccessed
        # so we don't render them twice (only 1 widget per element)
        self._elements_next: Set[Element] = set()

        # once reconcilidated, al elements moves here.
        self._elements: Set[Element] = set()

        # widgets created as side effect (like Layout and Style)
        # key is the widget model id (because some widgets are not hashable, like plotly)
        # We keep track of this to make sure we clean up all widgets.
        self._orphans: Dict[str, Set[str]] = {}
        # for detecting stale elements used get_widget
        self._old_element_ids: Set[int] = set()

    def close(self):
        with self.thread_lock:
            self._remove_element(self.element, default_key="/", parent_key=ROOT_KEY)
            assert self.context is self.context_root
        if self.container:
            self.container.close()
            if isinstance(self.container, widgets.DOMWidget):
                self.container.layout.close()
        if self._elements:
            raise RuntimeError(f"Element not cleaned up: {self._elements}")
        if self._orphans:
            orphan_widgets = set([widgets.Widget.widgets[k] for k in self._orphans])
            raise RuntimeError(f"Orphan widgets not cleaned up: {orphan_widgets}")

    def state_get(self, context: Optional[ComponentContext] = None):
        if context is None:
            context = self.context_root
        data = {}
        data["state"] = context.state
        if context.children:
            children_state = data["children"] = {}
            for name, context in context.children.items():
                children_state[name] = self.state_get(context)
        return data

    def state_set(self, context: ComponentContext, state):
        context.state = state.get("state", {})
        for name, state in state.get("children", {}).items():
            context.children_next[name] = ComponentContext(parent=context)
            self.state_set(context.children_next[name], state)

    def use_memo(self, f, args, kwargs, debug_name: str = None):
        assert self.context is not None
        if args is None:
            args = tuple()
        if kwargs is None:
            kwargs = {}
        name = debug_name or "no-name"
        if len(self.context.memo) <= self.context.memo_index:
            self.context.memo_index += 1
            value = f(*args, **kwargs)
            memo = (value, (args, kwargs))
            self.context.memo.append(memo)
            logger.info("Initial memo = %r for index %r (debug-name: %r)", memo, self.context.memo_index - 1, name)
            return value
        else:
            memo = self.context.memo[self.context.memo_index]
            value, dependencies = memo
            if dependencies == (args, kwargs):
                logger.info("Got memo hit = %r for index %r (debug-name: %r)", memo, self.context.memo_index, name)
            else:
                logger.info("Replace memo with = %r for index %r (debug-name: %r)", memo, self.context.memo_index, name)
                value = f(*args, **kwargs)
                memo = (value, (args, kwargs))
                self.context.memo[self.context.memo_index] = memo
            self.context.memo_index += 1
            return value

    def use_state(self, initial, key: str = None, eq: Callable[[Any, Any], bool] = None) -> Tuple[T, Callable[[T], T]]:
        assert self.context is not None
        if key is None:
            key = str(self.context.state_index)
            self.context.state_index += 1
        if key not in self.context.state:
            self.context.state[key] = initial
            logger.info("Initial state = %r for key %r (%r)", initial, key, id(self.context))
            return initial, self.make_setter(key, self.context, eq)
        else:
            state = self.context.state[key]
            logger.info("Got state = %r for key %r (%r)", state, key, id(self.context))
            return state, self.make_setter(key, self.context, eq)

    def make_setter(self, key, context: ComponentContext, eq: Callable[[Any, Any], bool] = None):
        def set(value):
            if callable(value):
                value = value(context.state[key])
            logger.info("Set state = %r for key %r (previous value was %r) (%r)", value, key, context.state[key], id(self.context))

            should_update = not eq(context.state[key], value) if eq is not None else context.state[key] != value

            if should_update:
                context.state[key] = value
                # TODO: enable
                # context.needs_render = True
                if self._state_changed is False:
                    self._state_changed = True
                    self._state_changed_reason = f"{key} changed"
                if not self._is_rendering:
                    self.render(self.element, self.container)
                else:
                    logger.info("No render phase triggered, already rendering")

        return set

    def force_update(self):
        if not self._is_rendering:
            self.render(self.element, self.container)

    def use_side_effect(self, effect: EffectCallable, dependencies=None):
        assert self.context is not None
        if len(self.context.effects) <= self.context.effect_index:
            self.context.effect_index += 1
            self.context.effects.append(Effect(effect, dependencies))
            logger.info("Initial effect = %r for index %r (%r)", effect, self.context.effect_index - 1, dependencies)
        else:
            previous_effect = self.context.effects[self.context.effect_index]
            # we always set it, even replacing it when we didn't execute it
            # in the consolidation phase we decide what to do (e.g. skip it)
            logger.info("Setting next effect = %r for index %r (%r)", effect, self.context.effect_index, dependencies)
            if previous_effect.executed:
                # line up...
                previous_effect.next = Effect(effect, dependencies)
            else:
                # replace
                self.context.effects[self.context.effect_index] = Effect(effect, dependencies)
            self.context.effect_index += 1

    def render(self, element: Element, container: widgets.Widget = None):
        # render + consolidate
        global _rc
        widget = None
        with self.thread_lock:
            try:
                _rc = self
                self.element = element
                main_render_phase = not self._is_rendering
                render_count = self.render_count  # make a copy
                self._state_changed = False
                self._state_changed_reason = None
                logger.info("Render phase: %r %r", self.render_count, "main" if main_render_phase else "(nested)")
                self.render_count += 1
                self._is_rendering = True
                # if we got called recursively, self.context is not the root context
                context_prev = self.context
                self.context = self.context_root
                self.context.root_element = element
                assert self.context is not None

                try:
                    self._elements_next = set()
                    self._render(element, "/", parent_key=ROOT_KEY)
                    self.first_render = False
                except BaseException:
                    self._is_rendering = False
                    raise

                if main_render_phase:
                    stable = False
                    render_counts = 0
                    while not stable:
                        # we started the rendering loop (main_render_phase is True), so we keep going
                        while self._state_changed:
                            logger.info("Entering nested render phase: %r", self._state_changed_reason)
                            self._state_changed = False
                            self._state_changed_reason = None
                            self._elements_next = set()
                            self._render(element, "/", parent_key=ROOT_KEY)
                            logger.info("Render done: %r %r", self._state_changed, self._state_changed_reason)
                            assert self.context is self.context_root
                            render_counts += 1
                            if render_counts > 50:
                                raise RuntimeError("Too many renders triggered, your render loop does not stop")
                        logger.debug("Render phase resulted in (next) elements:")
                        for el in self._elements_next:
                            logger.debug("\t%r", el)

                        logger.debug("Current elements:")
                        for el in self._elements:
                            logger.debug("\t %r", el)

                        widget = self._reconsolidate(element, default_key="/", parent_key=ROOT_KEY)
                        if self._elements_next:
                            raise RuntimeError(f"Element not reconsolidated: {self._elements_next}")
                        logger.debug("Reconsolidate phase resulted in elements:")
                        for el in self._elements:
                            logger.debug("\t%r", el)
                        # RESET
                        assert self.context is self.context_root
                        assert widget in self._widgets.values()
                        if self.last_root_widget is None:
                            self.last_root_widget = widget
                        else:
                            if container is None:
                                if self.last_root_widget != widget:
                                    raise ValueError(
                                        "You are not using a container, and the root component returned a new widget,"
                                        "make sure your root component always returns the same component type"
                                    )
                        if container:
                            container.children = [widget]

                        if self._state_changed:
                            logger.info("During consolidation, a stage changed was triggered")
                            stable = False
                        else:
                            stable = True

                    self._is_rendering = False
                self.context = context_prev
                logger.info("Done with render phase: %r", render_count)
            except Exception as e:
                if DEBUG:
                    # construct a fake traceback (showing how the elements were constructed)
                    if not self.tracebacks:
                        raise
                    # copy it, and we need with_traceback for unknown reasons not to cause
                    # an infinite loop
                    e_original = copy.copy(e).with_traceback(e.__traceback__)
                    tb_next = None

                    # last item is the top of the stack
                    for tb in self.tracebacks:
                        # make a copy, so we do not mutate the original traceback
                        tb = TracebackType(tb_next=tb_next, tb_frame=tb.tb_frame, tb_lasti=tb.tb_lasti, tb_lineno=tb.tb_lineno)
                        tb_next = tb

                    if TRACEBACK_ORIGINAL:
                        raise e.with_traceback(tb_next) from e_original
                    else:
                        raise e.with_traceback(tb_next)
                else:
                    raise

            finally:
                _rc = None  # type: ignore
            return widget

    def _render(self, element: Element, default_key: str, parent_key: str):
        if not isinstance(element, Element):
            raise TypeError(f"Expected element, not {element}")
        # for tracking stale data/elements when using get_widget
        self._old_element_ids.add(id(element))
        context = self.context
        assert context is not None

        if default_key == "/":
            # if this is the root element, reset
            context.used_keys.clear()
            default_key = element.component.name + "/"

        el = element
        # if we did not define a custom key, use the default key
        key = el._key
        if key is None:
            key = default_key

        logger.debug("Render: (%s,%s)  - %r", parent_key, key, element)

        if key in context.used_keys:
            if DEBUG:
                self.tracebacks.append(el.traceback)
            raise KeyError(f"Duplicate key {key!r}")
        context.used_keys.add(key)
        # if an element is used in multiple places, we only render it once
        if el in self._elements_next:
            # we already rendered it
            logger.debug("Render: Already rendered")
            return
        context.elements_next[key] = el
        self._elements_next.add(el)

        if isinstance(el.component, ComponentWidget):
            assert not el.args, "no positional args supported for widgets"

        if el.args or el.kwargs:
            # do this conditionally to make logs cleaner
            logger.debug("Render: arguments... (children of %s,%s)", parent_key, key)
            # we render the argument in the parent context
            self._visit_children(el, key, parent_key, self._render)
            assert self.context is context
            logger.debug("Render: arguments done (children of %s,%s)", parent_key, key)

        if isinstance(el.component, ComponentFunction):
            # call the function, and recurse into, until we hit leafs
            # find a context from previous reconsolidation phase, or otherwise the previous render run
            context_previous = context.children.get(key, context.children_next.get(key))
            parent_context = context
            del context
            if context_previous is not None:
                # We could reuse the same context
                if context_previous.root_element is None:
                    # this happens when we already created a context (with state) using state_set()
                    context = context_previous
                    logger.debug("Render: Previous element was None, so we reuse the ComponentContext")
                else:
                    # except when the type has changed
                    assert context_previous.invoke_element is not None
                    if not same_component(context_previous.invoke_element.component, el.component):
                        logger.debug("Render: Not the same component, we just copy the children and elements of the ComponentContext")
                        # The old context is cleaned up in the reconciliation phase
                        context = ComponentContext(parent=parent_context)
                    else:
                        logger.debug("Render: Same component: %r", el.component)
                        context = context_previous
                        context.parent = parent_context
                        # TODO: only render dirty components
                        # if not context_previous.needs_render:
                        #     # nothing changed
                        #     logger.info("skipping rendering of %s", key)
                        #     return
            else:
                logger.debug("Render: New ComponentContext")
                context = ComponentContext(parent=parent_context)
            context.invoke_element = el
            assert context.parent is not None
            self.container_adders = []
            logger.debug("Render: Enter context %r and excuting component function %r", key, el.component.f)
            self.context = context
            render_count = self.render_count
            try:
                context.state_index = 0
                context.effect_index = 0
                context.memo_index = 0
                # Now, we actually execute the render function, and get
                # back the root element
                try:
                    root_element: Element = el.component.f(*el.args, **el.kwargs)
                except Exception as e:
                    if DEBUG:
                        # we might be interested in the traceback inside the call...
                        if len(self.tracebacks) == 0:
                            assert e.__traceback__ is not None
                            traceback = cast(TracebackType, e.__traceback__)
                            if traceback.tb_next:  # is there an error inside the call
                                self.tracebacks.append(traceback.tb_next)
                        self.tracebacks.append(el.traceback)

                    raise

                if self.render_count != render_count:
                    raise RuntimeError("Recursive render detected, possible a bug in react")
                context.root_element = root_element
                new_parent_key = join_key(parent_key, key)
                self._render(root_element, "/", parent_key=new_parent_key)  # depth first
                # only expose to parent when no error occurs
                context.parent.children_next[key] = context
            finally:
                assert context.parent is parent_context
                self.context = context.parent
            assert context is not None

    def _reconsolidate(self, el: Element, default_key: str, parent_key: str):
        # we don't use default_key, but we want the same signature for the visitor pattern
        kwargs = el.kwargs.copy()
        key = el._key
        if key is None:
            if default_key == "/":
                default_key = el.component.name + "/"
            key = default_key
        assert key is not None
        logger.debug("Reconsolidate: (%s,%s) %r", parent_key, key, el)
        context = self.context
        assert context is not None

        el_prev = context.elements.get(key)

        already_reconsolidated = el in self._elements
        if already_reconsolidated and el is not self.element:

            logger.debug("Reconsolidate: Using existing widget (prev = %r)", el_prev)
            logger.debug("Current:")
            for el_ in self._elements:
                logger.debug("\t%r", el_)
            logger.debug("Next:")
            for el_ in self._elements_next:
                logger.debug("\t%r", el_)

            return self._widgets[el]

        try:
            if isinstance(el.component, ComponentFunction):
                new_parent_key = join_key(parent_key, key)
                try:
                    if el.args or el.kwargs:
                        # do this conditionally to make logs cleaner
                        logger.debug("Reconsolidate: arguments... (children of %s,%s)", parent_key, key)
                        self._visit_children(el, key, parent_key, self._reconsolidate)
                        assert self.context is context
                        logger.debug("Reconsolidate: arguments done (children of %s,%s)", parent_key, key)

                    child_context_prev = context.children.get(key)
                    child_context = context.children_next[key]
                    if child_context_prev is not None and child_context_prev is not child_context:
                        assert el_prev is not None, "prev child is not None, but element is"
                        # this happens when the component type changes
                        # this is not always true, it could be that there are two renders phases before this happened
                        # where the first updated the invoke_element, and the second changed the component
                        # assert child_context_prev.invoke_element is el_prev
                        self._remove_element(el_prev, default_key="/", parent_key=parent_key)

                    logger.debug("Reconsolidate: enter context %r", new_parent_key)
                    self.context = child_context
                    assert child_context.root_element
                    elements_now = dict(child_context.elements_next)
                    elements = dict(child_context.elements)

                    widget = self._reconsolidate(child_context.root_element, "/", new_parent_key)

                    self._widgets[el] = widget
                    removed = set(elements) - set(elements_now)
                    if removed:
                        logger.info("elements to be removed: %r", removed)
                    if removed:
                        for key_remove in removed:
                            el_remove = elements[key_remove]
                            self._remove_element(el_remove, key_remove, parent_key)
                    for effect_index, effect in enumerate(child_context.effects):
                        effect()
                        if effect.next:
                            # if we have a next, it means that effect itself is executed
                            # TODO: custom equals
                            if effect.next.dependencies is not None and effect.dependencies == effect.next.dependencies:
                                logger.info("No need to add effect, dependencies are the same (%r)", effect.dependencies)
                                # not needed, just remove the reference
                                effect.next = None
                            else:
                                # dependencies changed, cleanup and execute next
                                if effect.cleanup:
                                    effect.cleanup()
                                effect = child_context.effects[effect_index] = effect.next
                                try:
                                    effect()
                                except Exception:
                                    # TODO: we might want to have a better stacktrace here
                                    logger.exception("Issue with effect in element %r", el)
                                    raise
                        else:
                            effect()

                    if child_context.elements_next:
                        # we can still have elements that are not used as a 'widget' in this context
                        # but we can still pass them down as an element.
                        unreferenced = []
                        for child_key, child_el in list(child_context.elements_next.items()):
                            if child_el not in self._elements:
                                unreferenced.append(child_el)
                            else:
                                child_context.elements[child_key] = child_context.elements_next.pop(child_key)
                                # if key in child_context
                        if unreferenced:
                            raise RuntimeError(f"Unused elements and unreferenced elements {unreferenced}")
                finally:
                    # restore context
                    self.context = context
                    logger.debug("Reconsolidate: leaving context %r", new_parent_key)
                context.children[key] = context.children_next.pop(key)

            else:
                assert isinstance(el.component, ComponentWidget)
                kwargs, custom_kwargs = el.split_kwargs(el.kwargs)
                if el.args:
                    raise TypeError("Widget element only take keyword arguments")

                logger.debug("Reconsolidate: arguments... (children of %s,%s)", parent_key, key)
                kwargs = self._visit_children_values(kwargs, key, parent_key, self._reconsolidate)
                assert self.context is context
                logger.debug("Reconsolidate: arguments done (children of %s,%s)", parent_key, key)

                before = set(widgets.Widget.widgets)
                widget_previous = None
                if el_prev is not None:
                    widget_previous = self._widgets[el_prev]
                if widget_previous is None:
                    # initial create
                    if el in self._widgets:
                        raise RuntimeError(f"Element ({el}) was already in self._widgets")
                        # logger.info("Using shared widget: %r", el)
                    else:
                        logger.info("Creating new widget: %r", el)
                        self._widgets[el] = self._create_widget(el, kwargs)
                        context.owns.add(el)
                    el.handle_custom_kwargs(self._widgets[el], custom_kwargs)
                elif type(widget_previous) is el.component.widget:
                    logger.info("Updating widget: %r  → %r", el_prev, el)
                    assert el_prev is not None
                    # TODO: remove event listeners while doing so
                    # assign to _widgets[el] first, before errors can occur
                    self._widgets[el] = widget_previous
                    self._update_widget(widget_previous, el, el_prev, kwargs)
                    context.owns.add(el)
                    context.owns.remove(el_prev)
                    el.handle_custom_kwargs(widget_previous, custom_kwargs)
                    assert el_prev is not el
                else:
                    assert el_prev is not None, "widget_previous is not None, but el_prev is"
                    logger.info("Replacing widget: %r → %r", el_prev, el)
                    assert el_prev in context.owns
                    self._remove_element(el_prev, key, parent_key=parent_key)
                    context.owns.remove(el_prev)
                    self._widgets[el] = self._create_widget(el, kwargs)
                    context.owns.add(el)
                    el.handle_custom_kwargs(self._widgets[el], custom_kwargs)
                after = set(widgets.Widget.widgets)
                widget = self._widgets[el]
                orphans = (after - before) - {widget.model_id}
                # widgets are not always hashable, so store the model_id
                orphan_widgets = set([widgets.Widget.widgets[k] for k in orphans])
                if orphans:
                    for orphan_widget in orphan_widgets:
                        # these are shared widgets
                        if orphan_widget.__class__.__name__ == "Template" and orphan_widget.__class__.__module__ == "ipyvue.Template":
                            orphans -= {orphan_widget.model_id}
                if widget.model_id not in self._orphans:
                    self._orphans[widget.model_id] = set()
                self._orphans[widget.model_id].update(orphans)
                # if is_root
            return self._widgets[el]
        except Exception as e:
            if DEBUG:
                # we don't care about the traceback of the root element
                if self.element is not el:
                    # we might be interested in the traceback inside the call...
                    if len(self.tracebacks) == 0:
                        assert e.__traceback__ is not None
                        traceback = cast(TracebackType, e.__traceback__)
                        if traceback.tb_next:  # is there an error inside the call
                            self.tracebacks.append(traceback.tb_next)
                    self.tracebacks.append(el.traceback)
            raise
        finally:
            # this marks the work as 'done'
            context.elements[key] = context.elements_next.pop(key)

            if el_prev in self._elements:
                self._elements.remove(el_prev)

            # move from _elemens_next to _elements
            assert el not in self._elements
            self._elements.add(el)

            assert el in self._elements_next
            self._elements_next.remove(el)

            logger.debug("Current:")
            for el_ in self._elements:
                logger.debug("\t%r", el_)
            logger.debug("Next:")
            for el_ in self._elements_next:
                logger.debug("\t%r", el_)

    def _create_widget(self, el: Element, kwargs):
        assert isinstance(el.component, ComponentWidget)
        try:
            widget = el.component.widget(**kwargs)
        except Exception:
            raise RuntimeError(f"Could not create widget {el.component.widget} with {kwargs}")
        return widget

    def _update_widget(self, widget: widgets.Widget, el: Element, el_prev: Element, kwargs) -> widgets.Widget:
        assert self.context is not None
        assert isinstance(el.component, ComponentWidget)
        assert isinstance(el_prev.component, ComponentWidget)
        with widget.hold_sync():
            for name, value in kwargs.items():
                self._update_widget_prop(widget, name, value)
                # if we previously gave an argument, but now we don't
                # we have to restore the default
                cls = widget.__class__
                traits = cls.class_traits()
                used_kwargs, _ = el_prev.split_kwargs(el_prev.kwargs)
                dropped_arguments = set(used_kwargs) - set(kwargs)
                for name in dropped_arguments:
                    value = traits[name].default()
                    self._update_widget_prop(widget, name, value)

    def _update_widget_prop(self, widget, name, value):
        setattr(widget, name, value)

    def _remove_element(self, el: Element, default_key: str, parent_key):
        key = el._key
        if key is None:
            if default_key == "/":
                default_key = el.component.name + "/"
            key = default_key
        assert key is not None
        assert self.context is not None
        context = self.context
        logger.info("Remove: (%s, %s) %r", parent_key, key, el)

        if el not in self._elements:
            return
        assert el in self._elements
        self._elements.remove(el)

        key_created = [key_ for key_, value in context.elements.items() if el == value][0]
        # TODO: why is this not key?
        # assert key_created == key
        if context is None:
            raise RuntimeError(f"Element {el} not found in context or parent context")

        self._visit_children(el, key, parent_key, self._remove_element)
        if isinstance(el.component, ComponentFunction):
            self.context = context.children[key_created]
            try:
                assert self.context.root_element is not None
                new_parent_key = join_key(parent_key, key)
                self._remove_element(self.context.root_element, "/", parent_key=new_parent_key)
            finally:
                # restore context
                self.context = context
            del context.children[key_created]
        else:
            widget = self._widgets[el]
            for orphan in self._orphans.get(widget.model_id, set()):

                orphan_widget = widgets.Widget.widgets.get(orphan)
                if orphan_widget:
                    orphan_widget.close()
            if widget.model_id in self._orphans:
                del self._orphans[widget.model_id]
            widget.close()
            del self._widgets[el]
        del context.elements[key_created]

    def _visit_children(self, el: Element, default_key: str, parent_key: str, f: Callable):
        assert self.context is not None
        key = el._key
        if key is None:
            key = default_key
        assert key is not None
        self._visit_children_values(el.kwargs, key, parent_key, f)
        self._visit_children_values(el.args, key, parent_key, f)

    def _visit_children_values(self, value: Any, key: str, parent_key: str, f: Callable):
        if isinstance(value, Element):
            return f(value, key, parent_key)
        elif isinstance(value, (list, tuple)):
            was_tuple = isinstance(value, tuple)
            values = [self._visit_children_values(v, f"{key}{index}/", parent_key, f) for index, v in enumerate(value)]
            if was_tuple:
                return tuple(values)
            return values
        elif isinstance(value, dict):
            return {k: self._visit_children_values(v, f"{key}{k}/", parent_key, f) for k, v in value.items()}
        else:
            return value


_rc = None


@overload
def render(
    element: Element[T], container: None = None, children_trait="children", handle_error: bool = True, initial_state=None
) -> Tuple[widgets.HBox, _RenderContext]:
    ...


@overload
def render(
    element: Element[T], container: None = None, children_trait="children", handle_error: bool = True, initial_state=None
) -> Tuple[widgets.Widget, _RenderContext]:
    ...


def render(element: Element[T], container: widgets.Widget = None, children_trait="children", handle_error: bool = True, initial_state=None):
    global _last_rc
    container = container or widgets.VBox()
    _rc = _RenderContext(element, container, children_trait=children_trait, handle_error=handle_error, initial_state=initial_state)
    _rc.render(element, _rc.container)
    _last_rc = _rc
    return container, _rc


def render_fixed(element: Element[T], handle_error: bool = True) -> Tuple[T, _RenderContext]:
    global _last_rc
    _rc = _RenderContext(element, handle_error=handle_error)
    widget = _rc.render(element)
    _last_rc = _rc
    return widget, _rc


def display(el: Element, mime_bundle: Dict[str, Any] = mime_bundle_default):
    import IPython.display  # type: ignore

    widget = make(el)

    data: Dict[str, Any] = {
        **mime_bundle_default,
        **mime_bundle,
        MIME_WIDGETS: {"version_major": 2, "version_minor": 0, "model_id": widget._model_id},
    }
    IPython.display.display(data, raw=True)
    widget._handle_displayed()


def make(el: Element, handle_error: bool = True):
    hbox = widgets.VBox(_view_count=0)
    _, rc = render(el, hbox, "children", handle_error=handle_error)
    return hbox


_last_interactive_vbox = None


def component_interactive(static=None, **kwargs):
    import IPython.display

    static = static or {}

    def make(f):
        global _last_interactive_vbox
        c = component(f)
        el0 = c(**{**static, **kwargs})
        container, rc = render(el0)

        def f_wrap(**kwargs):
            element = c(**{**static, **kwargs})
            rc.render(element)

        control = widgets.interactive(f_wrap, **kwargs)
        control.update()
        result = widgets.VBox([control, container])
        _last_interactive_vbox = result
        IPython.display.display(result)
        return result

    return make
